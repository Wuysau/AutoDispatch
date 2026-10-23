from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .model import Blocked, digest
from .safety import redact
from .process_tree import ProcessTree
from .execution import ExecutionTrace, redact as redact_output

ENV_KEYS = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
            "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOME", "LANG", "LC_ALL"}


def clean_env():
    result = {k: v for k, v in os.environ.items() if k.upper() in ENV_KEYS}
    result.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", GIT_TERMINAL_PROMPT="0",
                  GCM_INTERACTIVE="Never", CI="1", NO_COLOR="1")
    return result


@dataclass
class Result:
    code: int
    output: str
    receipt: dict


def terminate_tree(p: subprocess.Popen):
    if os.name == "nt":
        subprocess.run(["taskkill.exe", "/PID", str(p.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    else:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    p.wait(timeout=15)


class Runner:
    def __init__(self, root: Path | None = None, store=None, max_requests=1):
        self.root, self.store, self.max_requests = root, store, max_requests

    def run(self, argv: list[str], cwd: Path, timeout: int, *, tid="doctor", stage="probe",
            agent=False, stdin: str | None = None, extra_env: dict | None = None,
            model: str | None = None, reasoning_effort: str | None = None) -> Result:
        if self.root and (self.root / "stop.request").exists():
            raise Blocked("Stop requested; no new subprocess launched")
        if not argv or any("\x00" in x for x in argv):
            raise Blocked("Invalid argv")
        if Path(argv[0]).suffix.lower() in {".cmd", ".bat", ".ps1"}:
            raise Blocked("Use native executable or node + JS entrypoint; shell shims forbidden")
        if agent:
            if self.store is None:
                raise Blocked("Agent launch without durable store")
            self.store.charge(self.max_requests)
        env = clean_env()
        if extra_env:
            env.update(extra_env)
        started = time.time()
        trace = None
        if self.root and self.store:
            try:
                trace = ExecutionTrace(self.root, tid, stage, started)
            except OSError:
                pass
        if self.store:
            self.store.update(tid, pending_launch=True, stage=stage, coordinator_pid=os.getpid(),
                              cwd=str(cwd), lease_until=started + timeout,
                              execution_id=trace.id if trace else None, stage_started_at=started)
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
            kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NEW_CONSOLE,
                      "startupinfo": startup}
        else:
            kwargs = {"start_new_session": True}
        launched_argv = ([sys.executable, str(Path(__file__).with_name("spawn_gate.py")), json.dumps(argv)]
                         if os.name == "nt" else argv)
        p = subprocess.Popen(launched_argv, cwd=cwd, env=env,
                             stdin=subprocess.PIPE if stdin is not None or os.name == "nt" else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False, **kwargs)
        try:
            tree = ProcessTree(p)
        except BaseException:
            terminate_tree(p)
            if p.stdin:
                p.stdin.close()
            p.stdout.close()
            raise
        if self.store:
            self.store.update(tid, pid=p.pid, pending_launch=False)
        chunks, size, too_large = [], [0], threading.Event()

        def read():
            while block := p.stdout.read1(4096):
                size[0] += len(block)
                if size[0] > 8 * 1024 * 1024:
                    too_large.set()
                    break
                chunks.append(block)
                if trace:
                    try:
                        trace.feed(block)
                    except Exception:
                        # Telemetry must never stop draining the CLI's pipe.
                        trace.disabled = True

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        def write():
            try:
                if os.name == "nt":
                    p.stdin.write(b"G")
                    p.stdin.flush()
                if stdin is not None:
                    p.stdin.write(stdin.encode("utf-8"))
                p.stdin.close()
            except BrokenPipeError:
                pass
        writer = None
        if stdin is not None or os.name == "nt":
            writer = threading.Thread(target=write, daemon=True)
            writer.start()
        timed_out = False
        stopped = False
        try:
            while p.poll() is None:
                if self.root and (self.root / "stop.request").exists():
                    stopped = True
                    terminate_tree(p)
                    break
                if time.time() - started >= timeout or too_large.is_set():
                    timed_out = True
                    terminate_tree(p)
                    break
                time.sleep(0.05)
        except BaseException:
            tree.close()
            terminate_tree(p)
            raise
        tree.close()
        reader.join(timeout=2)
        if reader.is_alive():
            # A descendant retained stdout: do not treat the stage as finished.
            timed_out = True
        else:
            p.stdout.close()
        if writer:
            writer.join(timeout=2)
            if not writer.is_alive() and not p.stdin.closed:
                p.stdin.close()
        code = 130 if stopped else 124 if timed_out else p.returncode
        if trace and not reader.is_alive():
            try:
                trace.finish(code)
            except Exception:
                trace.disabled = True
        output = redact_output(b"".join(chunks).decode("utf-8", errors="replace"))
        receipt = {"argv": [redact(x) for x in argv], "cwd": str(cwd), "pid": p.pid,
                   "exit_code": code, "started_at": started, "finished_at": time.time(),
                   "timeout": timed_out, "output_limit": too_large.is_set(),
                   "usage": "UNKNOWN", "model": model or "UNKNOWN", "reasoning_effort": reasoning_effort or 'DEFAULT', "stopped": stopped,
                   "task_id": tid, "stage": stage, "elapsed_seconds": time.time() - started}
        if trace:
            receipt["execution_id"] = trace.id
        if agent:
            totals = {}
            models = set()
            for line in output.splitlines():
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, dict):
                    continue
                if isinstance(event.get("model"), str):
                    models.add(event["model"])
                usage = event.get("usage")
                if isinstance(usage, dict):
                    for key in ("input_tokens", "output_tokens", "cached_input_tokens", "cost_usd"):
                        if isinstance(usage.get(key), (int, float)) and not isinstance(usage[key], bool):
                            totals[key] = totals.get(key, 0) + usage[key]
            if totals:
                receipt["usage"] = totals
            if models:
                receipt["reported_models"] = sorted(models)
        if self.root:
            folder = self.root / "logs" / tid
            folder.mkdir(parents=True, exist_ok=True)
            base = folder / f"{stage}-{uuid.uuid4().hex}"
            log = base.with_suffix(".log")
            log.write_text(output, encoding="utf-8", newline="\n")
            receipt.update(log=str(log), log_sha256=digest(log.read_bytes()))
            record = base.with_suffix(".json")
            record.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
            receipt["receipt_path"] = str(record)
        if self.store:
            self.store.update(tid, pid=None, pending_launch=False, last_exit_code=code,
                              lease_until=None, last_receipt=receipt)
        return Result(code, output, receipt)


def checked(result: Result) -> str:
    if result.code != 0:
        raise Blocked(f"Command failed ({result.code}): {result.output[-1200:]}")
    return result.output.strip()

"""Bounded, redacted subprocess telemetry. It never controls scheduling policy."""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from .control import atomic_json
from .model import within
from .safety import redact as redact_base
from .store import process_alive

MAX_LINE = 65536
MAX_TAIL = 256 * 1024
MAX_EVENTS = 200
EXECUTION_ID = re.compile(r"[0-9a-f]{32}")
AUTH_HEADER = re.compile(r'''(?i)\bauthorization["']?\s*[:=]\s*["']?(?:bearer|basic)\s+[^\s"'\\,;}\]]+''')


def redact(text: str) -> str:
    # The generic scanner sees only the scheme in a whitespace-separated header.
    text = AUTH_HEADER.sub("Authorization: [REDACTED]", text)
    return redact_base(text).encode('utf-8', errors='replace').decode('utf-8')


def sanitized(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [sanitized(item) for item in value]
    if isinstance(value, dict):
        # Redact sensitive JSON fields as a unit, keeping the JSON structure valid.
        return {key: "[REDACTED]" if re.search(r"(?i)^(api[_-]?key|secret|password|authorization|access[_-]?token)$", key)
                else sanitized(item) for key, item in value.items()}
    return value


def describe(line: str) -> tuple[str, str, str] | None:
    """Decode public CLI events; deliberately do not expose reasoning events."""
    try:
        event = sanitized(json.loads(line))
    except (ValueError, RecursionError):
        line = redact(line)
        return "output", line[:240], line[:8192]
    if not isinstance(event, dict):
        line = redact(line)
        return "output", line[:240], line[:8192]
    line = json.dumps(event, ensure_ascii=False)
    kind = str(event.get("type", "output"))
    part = event.get("part") or event.get("item") or {}
    if not isinstance(part, dict):
        part = {}
    if "reasoning" in kind or str(part.get("type")) in {"reasoning", "analysis"}:
        return None
    if kind in {"error", "turn.failed"}:
        error = event.get("error", {})
        message = error.get("message", "执行器报告错误") if isinstance(error, dict) else str(error)
        return "error", str(message)[:240], line[:8192]
    if kind in {"step_start", "turn.started"}:
        return kind, "步骤开始，等待工具事件", line[:8192]
    if kind in {"step_finish", "turn.completed"}:
        return kind, "步骤结束，等待后续事件", line[:8192]
    state = part.get("state", {})
    if not isinstance(state, dict):
        state = {}
    tool = str(part.get("tool") or part.get("type") or "")
    inputs = state.get("input", {})
    if not isinstance(inputs, dict):
        inputs = {}
    if kind == "tool_use" or tool in {"command_execution", "file_change", "mcp_tool_call"}:
        target = (inputs.get("command") or inputs.get("filePath") or inputs.get("path")
                  or part.get("command") or part.get("changes") or "")
        status = state.get("status") or part.get("status") or kind
        return "tool", f"{tool} · {status} · {target}"[:240], line[:8192]
    if kind == "text" or tool == "agent_message":
        content = str(part.get("text", ""))
        return "message", content[:240], content[:8192]
    return kind, kind, line[:8192]


class ExecutionTrace:
    def __init__(self, root: Path, tid: str, stage: str, started: float):
        self.id = uuid.uuid4().hex
        self.folder = within(root, f"executions/{tid}/{self.id}")
        self.folder.mkdir(parents=True, exist_ok=True)
        self.buffer = bytearray()
        self.discarding = False
        self.disabled = False
        self.summary = {"id": self.id, "stage": stage, "startedAt": int(started * 1000),
                        "lastOutputAt": None, "lastActivityAt": None, "currentOperation": None,
                        "finishedAt": None, "exitCode": None, "outputBytes": 0, "sequence": 0}
        self.emit("process", "正在启动受管进程")
        self.save()

    def save(self):
        if self.disabled:
            return
        try:
            atomic_json(self.folder / "status.json", self.summary)
        except OSError:
            self.disabled = True  # Monitoring cannot kill an authorized subprocess.

    def emit(self, kind: str, message: str, detail: str = ""):
        if self.disabled:
            return
        self.summary["sequence"] += 1
        at = int(time.time() * 1000)
        row = {"seq": self.summary["sequence"], "time": at, "kind": kind,
               "message": redact(message), "detail": redact(detail)}
        try:
            with (self.folder / "events.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            if kind not in {"process", "exit", "telemetry"}:
                self.summary["lastActivityAt"] = at
                if kind in {"tool", "message", "output", "error", "step_start", "step_finish", "turn.started", "turn.completed"}:
                    self.summary["currentOperation"] = row["message"]
        except OSError:
            self.disabled = True

    def feed(self, block: bytes):
        if self.disabled:
            return
        self.summary["outputBytes"] += len(block)
        self.summary["lastOutputAt"] = int(time.time() * 1000)
        # Redact only complete lines: secrets split across pipe reads never reach disk.
        for piece in block.splitlines(keepends=True):
            complete = piece.endswith(b"\n")
            if not self.discarding:
                self.buffer.extend(piece)
                if len(self.buffer) > MAX_LINE:
                    self.buffer.clear()
                    self.discarding = True
                    self.emit("telemetry", "输出行过长，已省略")
            if complete:
                if not self.discarding:
                    self.line(bytes(self.buffer))
                self.buffer.clear()
                self.discarding = False
        self.save()

    def line(self, raw: bytes):
        line = raw.decode("utf-8", errors="replace").strip()
        if line:
            item = describe(line)
            if item:
                self.emit(*item)

    def finish(self, code: int):
        if self.disabled:
            return
        if self.buffer and not self.discarding:
            self.line(bytes(self.buffer))
        self.buffer.clear()
        self.summary.update(finishedAt=int(time.time() * 1000), exitCode=code)
        self.emit("exit", f"受管命令已结束 · exit {code}")
        self.save()


def execution_folder(root: Path, tid: str, execution_id: str) -> Path:
    if not EXECUTION_ID.fullmatch(execution_id):
        raise ValueError("Invalid execution ID")
    return within(root, f"executions/{tid}/{execution_id}")


def read_summary(root: Path, tid: str, record: dict) -> dict:
    result = {"id": record.get("execution_id"), "stage": record.get("stage") or record.get("state", "PENDING"),
              "startedAt": int(record["stage_started_at"] * 1000) if record.get("stage_started_at") else None,
              "lastOutputAt": None, "lastActivityAt": None, "currentOperation": None,
              "finishedAt": None, "exitCode": None, "outputBytes": 0, "sequence": 0,
              "telemetry": "unavailable"}
    if result["id"]:
        try:
            folder = execution_folder(root, tid, result["id"])
            data = json.loads(within(folder, "status.json").read_text(encoding="utf-8"))
            if data.get("id") == result["id"]:
                result.update(data, telemetry="live")
        except (OSError, ValueError):
            pass
    pid = record.get("pid")
    coordinator = record.get("coordinator_pid")
    result.update(pid=pid, coordinatorPid=coordinator)
    if record.get("pending_launch"):
        process = "starting"
    elif pid:
        process = "alive" if process_alive(pid) else "exited"
    elif result["finishedAt"]:
        process = "finished"
    elif record.get("worker_active") and process_alive(coordinator):
        process = "between_commands"
    else:
        process = "unknown"
    result["processState"] = process
    return sanitized(result)


def read_events(root: Path, tid: str, execution_id: str | None, after: int = 0) -> tuple[list, bool]:
    if not execution_id:
        return [], False
    try:
        path = within(execution_folder(root, tid, execution_id), "events.jsonl")
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - MAX_TAIL))
            if size > MAX_TAIL:
                stream.readline()  # Skip a partial first row.
            raw = stream.read(MAX_TAIL)
    except (OSError, ValueError):
        return [], False
    rows = []
    for line in raw.splitlines():
        try:
            row = sanitized(json.loads(line.decode("utf-8", errors="replace")))
            if isinstance(row, dict) and isinstance(row.get("seq"), int) and row["seq"] > after:
                rows.append(row)
        except ValueError:
            continue
    rows = rows[-MAX_EVENTS:]
    return rows, bool(rows and rows[0]["seq"] > after + 1)

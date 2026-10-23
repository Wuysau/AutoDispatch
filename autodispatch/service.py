from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .control import atomic_json, status
from .doctor import diagnose
from .model import Blocked, Plan, validate_spec
from .safety import require_approval, redact, approval_fingerprint
from .store import CoordinatorLock, Store
from .handoff import service_lease
from .workers import worker_capacity


def admission(c, approval_path, diagnosis=None):
    """Non-destructive real admission. Persist facts even when no Agent is admissible."""
    plan = Plan(Path(c["repo"]), c["pins"])
    validate_spec(plan.repo, c["manifest_sha256"])
    report = diagnosis or diagnose(c, online=c.get("integration_target") != "local-dev")
    blockers = [x for x in report["checks"] if x["status"] == "BLOCKED"]
    try:
        approved = require_approval(c, approval_path)
    except (Blocked, OSError, ValueError, KeyError) as exc:
        approved = None
        blockers.append({"check": "execution_authorization", "status": "BLOCKED", "detail": redact(str(exc))})
    root = Path(c["state_dir"])
    report.update(checked_at=time.time(), blockers=blockers, status="BLOCKED" if blockers else "ADMITTED")
    with CoordinatorLock(root):
        store = Store(root)
        try:
            records = store.records()
            for task in plan.tasks.values():
                if task.id in records:
                    continue
                state = "WAITING_HUMAN_APPROVAL" if task.id == "IP-00" else "PENDING"
                reason = ("Existing IP-00 lacks a verified merged remote PR; no reimplementation" if task.id == "IP-00"
                          else "Await current remote dependency verification")
                extra = {"branch": "task/ip-00"} if task.id == "IP-00" else {}
                store.update(task.id, state, executor=task.agent, dependencies=list(task.dependencies), reason=reason,
                             requested_model=c.get("codex_model" if task.agent == "Codex" else "opencode_model") or "UNKNOWN",
                             automatic_retries=0, **extra)
            atomic_json(root / "admission.json", report)
        finally:
            store.close()
    return report, approved


def serve(c, config_path, approval_path, cycles=1, poll_seconds=60, wait_for_idle=False, wait_timeout=4200):
    if not 1 <= cycles <= 1440 or not 1 <= poll_seconds <= 3600:
        raise Blocked("Finite service cycle and polling limits required")
    from .engine import execute
    root = Path(c["state_dir"])
    # Separate service lock covers idle/admission intervals, beyond the execution lock.
    with service_lease(root, wait_for_idle, wait_timeout):
        if (root / "stop.request").exists():
            return {"status": "STOPPED", "reason": "Existing stop request; use resume to acknowledge it"}
        service = {"pid": os.getpid(), "nonce": uuid.uuid4().hex, "state": "RUNNING", "started_at": time.time(),
                   "configuration_sha256": approval_fingerprint(c), "max_tasks": c["max_tasks"],
                   "max_workers": worker_capacity(c, Plan(Path(c['repo']), c['pins'])),
                   'worker_mode': c['max_workers'], "max_agent_requests": c["max_agent_requests"],
                   "command": [sys.executable, "-m", "autodispatch", "--config", str(config_path), "serve", "--execute",
                               "--approval", str(approval_path), "--max-cycles", str(cycles), "--poll-seconds", str(poll_seconds)]}
        atomic_json(root / "service.json", service)
        outcome = {"status": "IDLE", "launched_tasks": 0}
        total_launched = 0
        try:
            for cycle in range(cycles):
                if (root / "stop.request").exists():
                    outcome = {"status": "STOPPED"}
                    break
                service.update(cycle=cycle + 1, heartbeat_at=time.time())
                atomic_json(root / "service.json", service)
                report, approved = admission(c, approval_path)
                if report["blockers"]:
                    outcome = {"status": "BLOCKED", "blockers": report["blockers"], "launched_tasks": 0}
                    break
                service['max_workers'] = worker_capacity(c, Plan(Path(c['repo']), c['pins']), approved.get('authorized_tasks', []))
                atomic_json(root / 'service.json', service)
                outcome = execute(c, approval_path, False, max_tasks=c["max_tasks"] - total_launched)
                total_launched += outcome.get("launched_tasks", 0)
                if outcome.get("budget_exhausted"):
                    outcome["status"] = "BUDGET_EXHAUSTED"
                    break
                if total_launched >= c["max_tasks"]:
                    outcome["status"] = "TASK_LIMIT_REACHED"
                    break
                # Pause when the DAG has no eligible work; no busy model/network loop.
                if not outcome.get("launched_tasks"):
                    failed = any(r.get("state") in {"BLOCKED", "FAILED"} for r in outcome.get("tasks", {}).values())
                    outcome["status"] = "BLOCKED" if failed or outcome.get('worker_configuration_blocked') else "WAITING_HUMAN_APPROVAL"
                    break
                until = time.monotonic() + poll_seconds
                while cycle + 1 < cycles and time.monotonic() < until and not (root / "stop.request").exists():
                    time.sleep(min(0.2, max(0, until - time.monotonic())))
        except (Blocked, OSError, ValueError, KeyError) as exc:
            outcome = {"status": "BLOCKED", "reason": redact(str(exc))}
        except BaseException:
            service.update(state="INTERRUPTED", finished_at=time.time())
            atomic_json(root / "service.json", service)
            raise
        finally:
            service.update(state=outcome.get("status", "FINISHED"), finished_at=time.time(), result=outcome)
            atomic_json(root / "service.json", service)
        return outcome


def start(c, config_path, approval_path, cycles=1, poll_seconds=60, wait_for_idle=False, wait_timeout=4200):
    if not 1 <= cycles <= 1440 or not 1 <= poll_seconds <= 3600 or not 1 <= wait_timeout <= 86400:
        raise Blocked("Finite service and handoff limits required")
    root = Path(c["state_dir"])
    snapshot = status(c)
    existing = snapshot["service"]
    handoff = snapshot.get("handoff", {})
    if handoff.get("state") == "WAITING_FOR_IDLE" and handoff.get("pid_alive"):
        raise Blocked("A successor service is already queued")
    if existing.get("state") == "RUNNING" and existing.get("pid_alive") and not wait_for_idle:
        raise Blocked("A service process is already running")
    if (root / "stop.request").exists():
        raise Blocked("Stop request persists; use resume before starting")
    root.mkdir(parents=True, exist_ok=True)
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    log = logs / f"service-{uuid.uuid4().hex}.log"
    argv = [sys.executable, "-m", "autodispatch", "--config", str(config_path.resolve()), "serve", "--execute",
            "--approval", str(approval_path.resolve()), "--max-cycles", str(cycles), "--poll-seconds", str(poll_seconds)]
    if wait_for_idle:
        argv += ["--wait-for-idle", "--wait-timeout", str(wait_timeout)]
    kwargs = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    with log.open("wb") as output:
        child = subprocess.Popen(argv, cwd=Path(__file__).resolve().parent.parent,
                                 stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, close_fds=True, **kwargs)
    launch = {"status": "QUEUED" if wait_for_idle else "STARTED", "pid": child.pid, "command": argv, "log": str(log), "started_at": time.time()}
    atomic_json(root / "last-launch.json", launch)
    return launch

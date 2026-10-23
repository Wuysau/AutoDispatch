"""Durable service status and cooperative stop; never kill an unverified PID."""
from __future__ import annotations

import json
import os
import time
import uuid
from collections import Counter
from pathlib import Path

from .model import Blocked
from .store import Store, process_alive, CoordinatorLock


def atomic_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def status(c):
    root = Path(c["state_dir"])
    service = {}
    if (root / "service.json").is_file():
        service = json.loads((root / "service.json").read_text(encoding="utf-8"))
        service["pid_alive"] = process_alive(service.get("pid"))
        if service.get("state") == "RUNNING" and not service["pid_alive"]:
            service["state"] = "INTERRUPTED"
    records = Store.snapshot(root)
    handoff = {}
    if (root / "handoff.json").is_file():
        handoff = json.loads((root / "handoff.json").read_text(encoding="utf-8"))
        handoff["pid_alive"] = process_alive(handoff.get("pid"))
        if handoff.get("state") == "WAITING_FOR_IDLE" and not handoff["pid_alive"]:
            handoff["state"] = "INTERRUPTED"
    return {"mode": "READ_ONLY", "service": service, "handoff": handoff, "tasks": records,
            "budget": {"agent_cli_limit": c["max_agent_requests"], "agent_cli_used": Store.usage_snapshot(root)},
            "counts": dict(Counter(r["state"] for r in records.values())),
            "stop_requested": (root / "stop.request").exists(), "logs": str(root / "logs")}


def request_stop(c):
    root = Path(c["state_dir"])
    root.mkdir(parents=True, exist_ok=True)
    with CoordinatorLock(root / "stop-lock"):
        atomic_json(root / "stop.request", {"nonce": uuid.uuid4().hex, "requested_at": time.time(), "requested_by_pid": os.getpid()})
    wsl_stops = []
    for tid, record in Store.snapshot(root).items():
        if record.get("wsl_worktree"):
            try:
                from .model import Plan
                from .process import Runner
                from .wsl_adapter import WslAdapter
                from types import SimpleNamespace
                plan = Plan(Path(c["repo"]), c["pins"])
                backend = SimpleNamespace(c=c, repo=Path(c["repo"]), infra=Runner())
                result = WslAdapter(backend).call(plan.tasks[tid], record, "stop")
                wsl_stops.append({"task_id": tid, "result": result})
            except (Blocked, OSError, ValueError, KeyError) as exc:
                wsl_stops.append({"task_id": tid, "error": str(exc)})
    return {"status": "STOP_REQUESTED", "detail": "Stops new work and interrupts owned subprocess trees; Git state is preserved.",
            "service": status(c)["service"], "wsl_stops": wsl_stops}


def acknowledge_stop(root: Path, observed: bytes | None):
    if observed is None:
        return False
    with CoordinatorLock(root / "stop-lock"):
        path = root / "stop.request"
        if path.exists() and path.read_bytes() == observed:
            path.unlink()
            return True
    return False

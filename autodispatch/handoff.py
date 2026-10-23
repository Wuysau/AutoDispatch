"""Wait for the previous finite batch without interrupting its workers."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import os
from pathlib import Path
import time
import uuid

from .control import atomic_json
from .model import Blocked
from .store import CoordinatorLock


@contextmanager
def service_lease(root: Path, wait=False, timeout=4200):
    if not 1 <= timeout <= 86400:
        raise Blocked("Handoff wait must have a finite timeout")
    record = {"pid": os.getpid(), "nonce": uuid.uuid4().hex, "state": "WAITING_FOR_IDLE",
              "started_at": time.time(), "wait_timeout_seconds": timeout}
    deadline = time.monotonic() + timeout
    lock = CoordinatorLock(root / "service-lock")
    acquired = False
    guard = CoordinatorLock(root / "handoff-lock") if wait else nullcontext()
    try:
        with guard:
            if wait:
                atomic_json(root / "handoff.json", record)
            while True:
                if (root / "stop.request").exists():
                    if wait:
                        record.update(state="STOPPED", finished_at=time.time())
                        atomic_json(root / "handoff.json", record)
                    raise Blocked("Stop requested while waiting for service ownership")
                try:
                    lock.__enter__()
                    acquired = True
                    break
                except Blocked:
                    if not wait:
                        raise
                    if time.monotonic() >= deadline:
                        record.update(state="TIMED_OUT", finished_at=time.time())
                        atomic_json(root / "handoff.json", record)
                        raise Blocked("Previous batch still owns the service; handoff wait expired")
                    time.sleep(min(0.2, max(0, deadline - time.monotonic())))
            if wait:
                record.update(state="STARTED", acquired_at=time.time())
                atomic_json(root / "handoff.json", record)
        yield
    finally:
        if acquired:
            lock.__exit__(None, None, None)

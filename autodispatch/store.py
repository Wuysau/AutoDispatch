from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from .model import Blocked, STATES


class CoordinatorLock:
    """OS lock, not a timestamp guess. Kernel releases it on coordinator death."""
    def __init__(self, root: Path):
        self.path = root / "coordinator.lock"
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise Blocked("Another coordinator owns this state directory") from exc
        return self

    def __exit__(self, *_):
        self.file.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file, fcntl.LOCK_UN)
        self.file.close()


def process_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            # ACCESS_DENIED is uncertainty, not evidence of exit.
            return kernel.GetLastError() != 87
        try:
            code = ctypes.c_ulong()
            return not kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(root / "state.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, task TEXT, at REAL, payload TEXT);
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def records(self):
        with self.lock:
            return {k: json.loads(v) for k, v in self.db.execute("SELECT id,payload FROM tasks")}

    def update(self, tid: str, state: str | None = None, **fields):
        with self.lock, self.db:
            row = self.db.execute("SELECT payload FROM tasks WHERE id=?", (tid,)).fetchone()
            data = json.loads(row[0]) if row else {"task_id": tid, "state": "PENDING"}
            if state:
                if state not in STATES:
                    raise ValueError(state)
                data["state"] = state
            data.update(fields, updated_at=time.time())
            self.db.execute("INSERT OR REPLACE INTO tasks VALUES(?,?)", (tid, json.dumps(data)))
            self.db.execute("INSERT INTO events(task,at,payload) VALUES(?,?,?)", (tid, time.time(), json.dumps(data)))
            return data

    def event(self, tid: str, **data):
        with self.lock, self.db:
            self.db.execute("INSERT INTO events(task,at,payload) VALUES(?,?,?)", (tid, time.time(), json.dumps(data)))

    def charge(self, maximum: int):
        with self.lock, self.db:
            row = self.db.execute("SELECT value FROM meta WHERE key='agent_requests'").fetchone()
            n = int(row[0]) if row else 0
            if n >= maximum:
                raise Blocked("Agent request quota exhausted; no automatic retry/reset")
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('agent_requests',?)", (str(n + 1),))
            return n + 1

    def history(self, tid: str):
        return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM events WHERE task=? ORDER BY seq", (tid,))]

    def agent_requests_used(self):
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key='agent_requests'").fetchone()
            return int(row[0]) if row else 0

    @staticmethod
    def usage_snapshot(root: Path):
        path = root / "state.sqlite3"
        if not path.is_file():
            return 0
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            row = db.execute("SELECT value FROM meta WHERE key='agent_requests'").fetchone()
            return int(row[0]) if row else 0
        finally:
            db.close()

    @staticmethod
    def snapshot(root: Path):
        path = root / "state.sqlite3"
        if not path.is_file():
            return {}
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            return {k: json.loads(v) for k, v in db.execute("SELECT id,payload FROM tasks")}
        finally:
            db.close()

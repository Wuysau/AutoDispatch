"""Local snapshot and narrow control API for the AutoDispatch UI.

GET  /api/autodispatch/snapshot              read-only view of the real state directory
POST /api/autodispatch/control  {action}     only "stop" is forwarded; pause/resume are refused
POST /api/autodispatch/tasks/IP-NN/retry     prioritize an existing eligible recovery job

Bound to 127.0.0.1 only. Database reads are cached by file signature, so an idle
dispatcher costs one stat() per poll instead of a full table read.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from .control import request_stop
from .model import Blocked, Plan
from .model_catalog import catalog
from .recovery_queue import RecoveryQueue, arrival_order, recovery_summary
from .safety import redact
from .store import Store, process_alive
from .execution import read_summary, read_events, EXECUTION_ID
from .safety import approval_fingerprint
from .workers import (dag_width, default_profile, model_options, save_profile, selected_profile,
                      settings_snapshot, validate_profile, worker_capacity)

PREFIX = "/api/autodispatch"
HOSTS = {"127.0.0.1", "localhost"}
MAX_BODY = 4096
MAX_LOGS = 200
TASK_ID = re.compile(r"IP-\d{2}")
SIGNED_FILES = ("state.sqlite3", "state.sqlite3-wal", "service.json", "stop.request")
UI_HOST_FILE = "ui-host.json"
UI_ALIVE_FILE = "ui-host-alive"

TASK_STATUS = {
    "MERGED_VERIFIED": "completed",
    "INTEGRATED_UNVERIFIED": "waiting",
    "RECOVERY_QUEUED": "waiting",
    "RECOVERY_RUNNING": "running",
    "RECOVERY_EXHAUSTED": "failed",
    "BLOCKED": "failed",
    "FAILED": "failed",
    "RUNNING": "running",
    "RED_READY": "running",
    "RED_VERIFIED": "running",
    "CODE_READY_VERIFY_PENDING": "running",
    "REVIEWING": "running",
    "WAITING_PR": "waiting",
    "WAITING_CI": "waiting",
    "WAITING_HUMAN_APPROVAL": "waiting",
    "PENDING": "pending",
    "READY": "pending",
}
PROGRESS = {"PENDING": 0, "READY": 0, "MERGED_VERIFIED": 100}


class ApiError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _signature(root: Path):
    parts = []
    for name in SIGNED_FILES:
        try:
            st = (root / name).stat()
            parts.append((name, st.st_mtime_ns, st.st_size))
        except FileNotFoundError:
            parts.append((name, 0, 0))
    return tuple(parts)


def _recent_events(root: Path, limit: int):
    path = root / "state.sqlite3"
    if not path.is_file():
        return []
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        return db.execute(
            "SELECT seq, task, at, json_extract(payload,'$.state'), json_extract(payload,'$.reason') "
            "FROM events ORDER BY at DESC, seq DESC LIMIT ?", (limit,)).fetchall()
    finally:
        db.close()


def _recovery_jobs(root: Path):
    path = root / 'state.sqlite3'
    if not path.is_file():
        return {}
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recovery_jobs'").fetchone():
            return {}
        return {tid: arrival_order(db, json.loads(raw))
                for tid, raw in db.execute('SELECT task,payload FROM recovery_jobs')}
    finally:
        db.close()


def _text(value, limit=300):
    if value is None:
        return None
    return redact(str(value))[:limit]


class DispatchApi:
    def __init__(self, c: dict, plan=None):
        self.c = c
        self.root = Path(c["state_dir"])
        self.ui_root = Path(c.get("ui_state_dir", c["state_dir"]))
        self.plan = plan
        self._lock = threading.Lock()
        self._key = None
        self._rows = ({}, [], {}, {})
        self._ui_lock = threading.Lock()
        self._ui_seq = time.time_ns() // 1000000
        self._ui_commands: list = []
        self._ui_requests: dict = {}

    def _task_plan(self):
        if self.plan is None:
            self.plan = Plan(Path(self.c["repo"]), self.c["pins"])
        return self.plan

    def _records(self):
        key = _signature(self.root)
        with self._lock:
            if key != self._key:
                self._rows = (Store.snapshot(self.root), _recent_events(self.root, MAX_LOGS),
                              _recovery_jobs(self.root), settings_snapshot(self.root))
                self._key = key
            return self._rows

    def _service(self):
        path = self.root / "service.json"
        if not path.is_file():
            return {}
        service = json.loads(path.read_text(encoding="utf-8"))
        service["pid_alive"] = process_alive(service.get("pid"))
        return service

    def _worker_limits(self, settings):
        plan = self._task_plan()
        scheduler = settings.get('scheduler', {})
        matches = scheduler.get('configuration_sha256') == approval_fingerprint(self.c)
        authorized = scheduler.get('authorized_tasks') if matches else None
        capacity = worker_capacity(self.c, plan, authorized)
        saved = scheduler.get('capacity')
        if matches and type(saved) is int and 0 <= saved <= capacity:
            capacity = saved
        mode = scheduler.get('mode', self.c.get('max_workers', 'auto')) if matches else self.c.get('max_workers', 'auto')
        return capacity, authorized, mode

    def snapshot(self) -> dict:
        plan = self._task_plan()
        records, events, jobs, settings = self._records()
        service = self._service()
        now = time.time()
        stop_requested = (self.root / "stop.request").exists()
        running = service.get("state") == "RUNNING" and service.get("pid_alive") and not stop_requested

        tasks = []
        for tid in sorted(plan.tasks):
            task = plan.tasks[tid]
            rec = records.get(tid, {})
            state = rec.get("state", "PENDING")
            status = TASK_STATUS.get(state, "pending")
            item = {
                "id": tid,
                "name": task.title,
                "status": status,
                "executor": rec.get("executor", task.agent),
                "stage": rec.get("stage") if status == "running" and rec.get("stage") else state.replace("_", " ").lower(),
                "deps": list(task.dependencies),
                "progress": PROGRESS.get(state),
                "updatedAt": int(rec.get("updated_at", 0) * 1000),
            }
            if status in {"pending", "waiting"}:
                unmet = [d for d in task.dependencies if records.get(d, {}).get("state") not in
                         {"MERGED_VERIFIED", "INTEGRATED_UNVERIFIED"}]
                if unmet:
                    item["blockedBy"] = unmet
            if status == "waiting":
                item["blockedReason"] = _text(rec.get("reason"))
            if status == "failed":
                item["lastError"] = _text(rec.get("reason")) or state
            if rec.get("worktree"):
                item["worktree"] = _text(rec["worktree"], 500)
            if status == "running" or rec.get("execution_id"):
                item["execution"] = read_summary(self.root, tid, rec)
            if tid in jobs:
                item['recovery'] = recovery_summary(jobs[tid], rec)
            elif rec.get("recovery"):
                item["recovery"] = rec["recovery"]
            tasks.append(item)

        max_workers, authorized, mode = self._worker_limits(settings)
        dag_max = dag_width(plan.tasks, (set(plan.tasks) if authorized is None else set(authorized)) - {'IP-00', 'IP-34'})
        active = []
        for task in tasks:
            rec = records.get(task['id'], {})
            owned = (rec.get('worker_active') and process_alive(rec.get('coordinator_pid'))) or process_alive(rec.get('pid'))
            legacy = 'worker_active' not in rec and task['status'] == 'running'
            if owned or legacy:
                active.append(task)
        active.sort(key=lambda t: (records[t['id']].get('worker_slot', max_workers + 1), t['id']))
        workers = []
        used = set()
        for t in active:
            rec = records[t['id']]
            slot = rec.get('worker_slot')
            editable = type(slot) is int and 1 <= slot <= max_workers and slot not in used
            if not editable:
                slot = next(s for s in range(1, len(plan.tasks) + 1) if s not in used)
            used.add(slot)
            official = catalog(self.c, t['executor']) if self.c.get('worker_model_selection') == 'official' else {
                'status': 'disabled', 'source': '', 'models': [], 'error': '当前配置尚未启用模型选择'}
            workers.append({
                "slot": slot,
                "executor": t['executor'], "taskId": t['id'],
                "elapsedSec": max(0, int(now - rec.get('worker_started_at', t['updatedAt'] / 1000))),
                'profile': rec.get('worker_profile', default_profile(self.c, t['executor'])),
                'nextProfile': selected_profile(self.c, settings, slot, t['executor']),
                'modelOptions': model_options(self.c, t['executor']),
                'configurable': editable and official['status'] == 'ready',
                'catalogStatus': official['status'], 'catalogSource': official['source'],
                'catalogUpdatedAt': official.get('updatedAt'), 'catalogError': official.get('error'),
            })

        issues = []
        for key, issue in settings.items():
            if not key.startswith('worker:issue:') or issue.get('configuration_sha256') != approval_fingerprint(self.c):
                continue
            slot, executor = issue['slot'], issue['executor']
            if not 1 <= slot <= max_workers:
                continue
            official = catalog(self.c, executor)
            profile = selected_profile(self.c, settings, slot, executor)
            issues.append({'slot': slot, 'executor': executor, 'taskId': None, 'elapsedSec': 0,
                           'profile': profile, 'nextProfile': profile, 'issue': issue['issue'],
                           'defaultProfile': default_profile(self.c, executor),
                           'modelOptions': model_options(self.c, executor), 'configurable': official['status'] == 'ready',
                           'catalogStatus': official['status'], 'catalogSource': official['source'],
                           'catalogUpdatedAt': official.get('updatedAt'), 'catalogError': official.get('error')})

        logs = []
        for seq, task, at, state, reason in events:
            level = "SUCCESS" if state == "MERGED_VERIFIED" else "ERROR" if state in {"BLOCKED", "FAILED", "RECOVERY_EXHAUSTED"} \
                else "WARN" if state and TASK_STATUS.get(state) == "waiting" else "INFO"
            message = (state or "event") + (f": {_text(reason)}" if reason else "")
            entry = {"id": str(seq), "time": int(at * 1000), "level": level,
                     "source": task or "dispatcher", "message": message}
            if task:
                entry["taskId"] = task
            logs.append(entry)

        started = service.get("started_at")
        ended = now if running or not service.get("finished_at") else service["finished_at"]
        return {
            "source": "live",
            "project": Path(self.c["repo"]).name,
            "subtitle": f"{self.c.get('integration_target', '?')} · {self.c.get('publication_mode', '?')}",
            "runState": "running" if running else "stopped",
            "runtimeSec": int(ended - started) if started else 0,
            "concurrency": max_workers,
            'dagMaxParallel': dag_max,
            'workerMode': 'auto' if mode == 'auto' else 'limited',
            "tasks": tasks,
            "workers": workers,
            'workerIssues': sorted(issues, key=lambda item: (item['slot'], item['executor'])),
            "logs": logs,
            "updatedAt": int(max((r.get("updated_at", 0) for r in records.values()), default=0) * 1000),
        }

    def execution(self, tid: str, query: str = "") -> dict:
        if not TASK_ID.fullmatch(tid) or tid not in self._task_plan().tasks:
            raise ApiError(404, "未知任务")
        params = parse_qs(query)
        try:
            after = int(params.get("after", ["0"])[0])
            if not 0 <= after <= 2**53 - 1:
                raise ValueError()
        except ValueError:
            raise ApiError(400, "无效日志游标")
        record = self._records()[0].get(tid, {})
        summary = read_summary(self.root, tid, record)
        requested = params.get("attempt", [summary["id"]])[0]
        if requested and (not EXECUTION_ID.fullmatch(requested) or requested != summary["id"]):
            # A stale poll must restart its cursor for a new command/attempt.
            after = 0
        events, truncated = read_events(self.root, tid, summary["id"], after)
        return {"taskId": tid, "execution": summary, "events": events,
                "cursor": events[-1]["seq"] if events else after, "truncated": truncated,
                "state": record.get("state", "PENDING")}

    def control(self, action) -> dict:
        if action == "stop":
            result = request_stop(self.c)
            return {"status": result["status"]}
        if action in {"pause", "resume"}:
            raise ApiError(409, "暂停/恢复没有后端语义；请用 CLI `resume --execute --approval` 显式恢复")
        raise ApiError(400, "未知操作")

    def retry(self, tid: str) -> dict:
        if not TASK_ID.fullmatch(tid) or tid not in self._task_plan().tasks:
            raise ApiError(404, "未知任务")
        if not (self.root / 'state.sqlite3').is_file():
            raise ApiError(409, '任务尚未进入失败恢复队列，请先检查失败现场')
        store = Store(self.root)
        try:
            job = RecoveryQueue(store).prioritize(tid)
        except Blocked as exc:
            raise ApiError(409, str(exc)) from exc
        finally:
            store.close()
        with self._lock:
            self._key = None
        return {'status': job['state'], 'taskId': tid, 'manualPriority': job['manual_priority']}

    def configure_worker(self, slot: int, body: dict) -> dict:
        if set(body) != {'executor', 'model', 'reasoningEffort'}:
            raise ApiError(400, '请提供执行器、模型和思考强度')
        settings = self._records()[3]
        capacity, _, _ = self._worker_limits(settings)
        if not 1 <= slot <= capacity:
            raise ApiError(400, 'Worker 编号超出 DAG 并发上限')
        executor = body.get('executor')
        profile = {'model': body.get('model'), 'reasoningEffort': body.get('reasoningEffort')}
        try:
            validate_profile(self.c, executor, profile)
        except Blocked as exc:
            raise ApiError(400, str(exc)) from exc
        store = Store(self.root)
        try:
            save_profile(store, self.c, slot, executor, profile)
        finally:
            store.close()
        with self._lock:
            self._key = None
        return {'status': 'SAVED', 'slot': slot, 'executor': executor, 'profile': profile}

    def models(self, refresh=False):
        return {executor: catalog(self.c, executor, refresh=refresh) for executor in ('Codex', 'OpenCode')}

    def set_ui_request(self, body: dict) -> dict:
        """页面把窗口尺寸/置顶/打开控制台的请求交给原生宿主执行。"""
        action = str(body.get("action") or "")
        if action not in {"size", "pin", "console", "hide", "show", "minimize"}:
            raise ApiError(400, "未知宿主操作")
        target = str(body.get("target") or "widget")
        if target not in {"widget", "console"}:
            raise ApiError(400, "未知宿主目标")
        request = {"action": action, "target": target, "at": time.time()}
        if action == "size":
            for key in ("width", "height"):
                value = body.get(key)
                if type(value) is not int or not 52 <= value <= 4096:
                    raise ApiError(400, "窗口尺寸须为 52–4096 的整数")
                request[key] = value
        if action == "pin":
            if type(body.get("value")) is not bool:
                raise ApiError(400, "置顶状态须为布尔值")
            request["value"] = body["value"]
        if action == "console" and body.get("task_id") is not None:
            tid = body["task_id"]
            if not isinstance(tid, str) or not TASK_ID.fullmatch(tid) or not 0 <= int(tid[3:]) <= 38:
                raise ApiError(400, "无效任务编号")
            request["task_id"] = tid
        with self._ui_lock:
            self._ui_seq += 1
            request["seq"] = self._ui_seq
            self._ui_requests[target] = request
            self._ui_commands = [*self._ui_commands[-63:], request]
            payload = {"seq": self._ui_seq, "requests": dict(self._ui_requests), "commands": self._ui_commands}
            self._publish_ui_requests(payload)
        return {"status": "ok", "seq": request["seq"]}

    def _publish_ui_requests(self, payload: dict) -> None:
        """把窗口请求写到状态目录，原生宿主在 UI 线程里读这个文件。"""
        try:
            root = self.ui_root
            root.mkdir(parents=True, exist_ok=True)
            path = root / UI_HOST_FILE
            tmp = root / (UI_HOST_FILE + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass

    def ui_state(self, heartbeat: bool = False) -> dict:
        with self._ui_lock:
            requests = dict(self._ui_requests)
            seq = self._ui_seq
        result = {}
        try:
            result = json.loads((self.ui_root / "ui-host-results.json").read_text(encoding="utf-8"))
            if time.time() - result.get("at", 0) > 3:
                result = {}
        except (OSError, ValueError):
            pass
        return {"seq": seq, "host": self._host_alive(), "requests": requests,
                "results": result.get("results", {}), "windows": result.get("windows", {})}

    def _host_alive(self) -> bool:
        """宿主在 UI 线程里刷新心跳文件，超过 3 秒没更新视为离线。"""
        try:
            path = self.ui_root / UI_ALIVE_FILE
            return (time.time() - path.stat().st_mtime) < 3.0
        except OSError:
            return False


CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


class _Handler(BaseHTTPRequestHandler):
    server_version = "AutoDispatchAPI"

    def log_message(self, *_):
        pass

    @property
    def api(self) -> DispatchApi:
        return self.server.api

    def _static(self, path: str):
        root = self.server.static_dir
        if root is None:
            return self._send(404, {"error": "not found"})
        rel = path.lstrip("/") or "index.html"
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return self._send(404, {"error": "not found"})
        if not target.is_file():
            target = root / "index.html"
        if not target.is_file():
            return self._send(404, {"error": "ui not built"})
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        if target.parent.name == "assets":
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in HOSTS

    def _send_cacheable(self, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        etag = '"' + hashlib.sha256(body).hexdigest()[:20] + '"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, call, cacheable=False):
        try:
            payload = call()
        except ApiError as exc:
            return self._send(exc.code, {"error": exc.message})
        except Blocked as exc:
            return self._send(503, {"error": redact(str(exc))})
        except Exception as exc:  # the UI must always get JSON, never a stack trace
            return self._send(500, {"error": "internal", "detail": redact(str(exc))})
        if cacheable:
            return self._send_cacheable(payload)
        self._send(200, payload)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden host"})
        path = urlsplit(self.path).path
        if path == f"{PREFIX}/snapshot":
            return self._dispatch(self.api.snapshot, cacheable=True)
        if path == f'{PREFIX}/models':
            return self._dispatch(self.api.models)
        match = re.fullmatch(rf"{re.escape(PREFIX)}/tasks/([^/]+)/execution", path)
        if match:
            return self._dispatch(lambda: self.api.execution(match.group(1), urlsplit(self.path).query))
        if path == f"{PREFIX}/ui/host":
            heartbeat = "heartbeat=1" in (urlsplit(self.path).query or "")
            return self._dispatch(lambda: self.api.ui_state(heartbeat))
        if path.startswith(PREFIX):
            return self._send(404, {"error": "not found"})
        self._static(path)

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden host"})
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._send(415, {"error": "application/json required"})
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 <= length <= MAX_BODY:
            return self._send(413, {"error": "body too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "invalid json"})
        if not isinstance(body, dict):
            return self._send(400, {"error": "object body required"})
        path = urlsplit(self.path).path
        if path == f"{PREFIX}/ui/host":
            return self._dispatch(lambda: self.api.set_ui_request(body))
        if path == f"{PREFIX}/control":
            return self._dispatch(lambda: self.api.control(body.get("action")))
        if path == f'{PREFIX}/models/refresh':
            return self._dispatch(lambda: self.api.models(refresh=True))
        worker_match = re.fullmatch(rf'{re.escape(PREFIX)}/workers/(\d+)/settings', path)
        if worker_match:
            return self._dispatch(lambda: self.api.configure_worker(int(worker_match.group(1)), body))
        match = re.fullmatch(rf"{re.escape(PREFIX)}/tasks/([^/]+)/retry", path)
        if match:
            return self._dispatch(lambda: self.api.retry(match.group(1)))
        self._send(404, {"error": "not found"})


class _Server(ThreadingHTTPServer):
    daemon_threads = True


def make_server(c: dict, port: int = 8765, plan=None, static_dir: Path | None = None) -> _Server:
    server = _Server(("127.0.0.1", port), _Handler)
    server.api = DispatchApi(c, plan)
    server.static_dir = static_dir.resolve() if static_dir else None
    return server


def serve_api(c: dict, port: int = 8765, static_dir: Path | None = None):
    if not 1024 <= port <= 65535:
        raise Blocked("API port must be between 1024 and 65535")
    server = make_server(c, port, static_dir=static_dir)
    print(json.dumps({"status": "LISTENING", "url": f"http://127.0.0.1:{port}/", "api": f"{PREFIX}/snapshot"},
                     ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()

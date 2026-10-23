from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .doctor import diagnose, planning
from .model import Blocked, load_config
from .safety import redact
from .store import Store
from .control import request_stop, status


def main(argv=None):
    p = argparse.ArgumentParser(description="AetherTrade AutoDispatch (dry-run unless --execute)")
    root = Path(__file__).resolve().parent.parent
    default = root / "config.local.json"
    p.add_argument("--config", type=Path, default=default if default.exists() else root / "config.example.json")
    sub = p.add_subparsers(dest="action", required=True)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--offline", action="store_true")
    sub.add_parser("plan")
    sub.add_parser("status")
    sub.add_parser("failures", help="Read the durable automatic recovery queue")
    sub.add_parser("stop")
    for name in ("start", "serve"):
        r = sub.add_parser(name)
        r.add_argument("--execute", action="store_true")
        r.add_argument("--approval", type=Path)
        r.add_argument("--max-cycles", type=int, default=1)
        r.add_argument("--poll-seconds", type=int, default=60)
        r.add_argument("--wait-for-idle", action="store_true")
        r.add_argument("--wait-timeout", type=int, default=4200)
    for name in ("run", "resume"):
        r = sub.add_parser(name)
        r.add_argument("--execute", action="store_true")
        r.add_argument("--approval", type=Path)
        r.add_argument("--max-workers", type=int)
        r.add_argument("--max-tasks", type=int)
    demo = sub.add_parser("demo")
    demo.add_argument("--output", type=Path, required=True)
    api = sub.add_parser("api")
    api.add_argument("--port", type=int, default=8765)
    api.add_argument("--ui-dist", type=Path)
    widget = sub.add_parser("widget")
    widget.add_argument("--url", default="http://127.0.0.1:8765/#/widget")
    desktop = sub.add_parser("desktop", help="Open the native desktop interface without starting tasks")
    desktop.add_argument("--demo", action="store_true", help="Use isolated simulated data")
    desktop.add_argument("--port", type=int, default=0)
    desktop.add_argument("--ui-dist", type=Path)
    args = p.parse_args(argv)
    try:
        if args.action == "desktop":
            from .desktop import run_desktop
            run_desktop(None if args.demo else load_config(args.config), demo=args.demo,
                        port=args.port, ui_dist=args.ui_dist)
            return 0
        c = load_config(args.config)
        if args.action == "doctor":
            result = diagnose(c, online=not args.offline)
        elif args.action == "status":
            result = status(c)
        elif args.action == "failures":
            result = {tid: dict(task_id=tid, **r['recovery']) for tid, r in
                      Store.snapshot(Path(c['state_dir'])).items() if r.get('recovery')}
        elif args.action == "stop":
            result = request_stop(c)
        elif args.action in {"start", "serve"}:
            if not args.execute:
                result = planning(c)
            else:
                if not args.approval:
                    raise Blocked("--execute requires --approval PATH")
                from .service import start, serve
                action = start if args.action == "start" else serve
                result = action(c, args.config, args.approval, args.max_cycles, args.poll_seconds,
                                args.wait_for_idle, args.wait_timeout)
        elif args.action == "plan" or (args.action in {"run", "resume"} and not args.execute):
            result = planning(c)
        elif args.action == "demo":
            from .fake import demo
            result = demo(c, args.output)
        elif args.action == "api":
            from .api import serve_api
            dist = args.ui_dist or root / "ui" / "dist"
            serve_api(c, args.port, dist if dist.is_dir() else None)
            return 0
        elif args.action == "widget":
            from .host import run_widget
            run_widget(args.url, c.get("state_dir"))
            return 0
        else:
            from .engine import execute
            if not args.approval:
                raise Blocked("--execute requires --approval PATH; nothing launched")
            if args.action == "resume":
                if status(c)["service"].get("state") == "RUNNING" and status(c)["service"].get("pid_alive"):
                    raise Blocked("Cannot resume while a service is running")
            result = execute(c, args.approval, args.action == "resume", args.max_workers, args.max_tasks)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (Blocked, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": redact(str(exc))}, ensure_ascii=False), file=sys.stderr)
        return 2

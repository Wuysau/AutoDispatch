from __future__ import annotations

from pathlib import Path
import json
import os

from .gitops import Git, GitHub
from .model import GRAPH, Plan, Blocked, validate_spec
from .process import Runner, checked
from .safety import approval_fingerprint, wsl_argv, wsl_path, require_approval
from .store import Store
from .codex_policy import platform_overrides


def diagnose(c: dict, online=True):
    repo = Path(c["repo"])
    checks = []

    def probe(name, fn):
        try:
            value = fn()
            checks.append({"check": name, "status": "PASS", "detail": value})
            return value
        except (Blocked, OSError, ValueError, KeyError) as exc:
            checks.append({"check": name, "status": "BLOCKED", "detail": str(exc)})
            return None

    runner = Runner()
    git = Git(runner, min(c["command_timeout_seconds"], 15))
    plan = probe("immutable_task_inputs", lambda: Plan(repo, c["pins"]))
    if plan:
        checks[-1]["detail"] = {"count": len(plan.tasks), "Codex": 34, "OpenCode": 5,
                                "plan_status": plan.plan_status, "graph_sha256": plan.sha}
    probe("approved_spec_23_files", lambda: len(validate_spec(repo, c["manifest_sha256"])))
    remote = probe("origin", lambda: git.text(repo, "remote", "get-url", "origin"))
    if remote != c["remote_url"]:
        checks.append({"check": "origin_identity", "status": "BLOCKED", "detail": "Unexpected origin"})
    probe("working_tree_clean", lambda: git.clean(repo) or "clean")
    local = probe("cached_origin_dev", lambda: git.sha(repo, "origin/dev"))
    selected = probe("local_dev", lambda: git.sha(repo, "dev")) if c.get("integration_target") == "local-dev" else None
    live = probe("live_origin_dev", lambda: git.remote_head(repo)) if online else None
    for name, command, required in [
        ("codex", c["codex_command"] + ["exec", "--help"], ["--sandbox", "--json", "--output-schema", "--output-last-message"]),
        ("opencode", c["opencode_command"] + ["run", "--help"], ["--agent", "--format", "--standalone"]),
    ]:
        def cli_probe(cmd=command, flags=required, cli=name):
            if cli == "opencode" and c["opencode_environment"] == "wsl":
                w = c["wsl"]
                wsl_path(w["repo"])
                cmd = w["opencode_command"] + ["run", "--help"]
                if not cmd[0].startswith("/home/"):
                    raise Blocked("Native WSL OpenCode executable required")
                cmd = wsl_argv(w["distribution"], w["repo"], cmd)
            out = checked(runner.run(cmd, repo, 15))
            if not all(flag in out for flag in flags):
                raise Blocked("Installed CLI lacks required flags")
            return "required flags present; auth/model not exercised"
        probe(name, cli_probe)
    if os.name == "nt":
        def sandbox_probe():
            argv = [*c["codex_command"], *platform_overrides(c), "sandbox", "-P", ":read-only",
                    "-C", str(repo), "git", "rev-parse", "HEAD"]
            observed = checked(runner.run(argv, repo, 15))
            if observed != git.sha(repo):
                raise Blocked("Codex native read-only sandbox returned a different commit")
            return {"backend": c["codex_windows_sandbox"], "read_only_git": "PASS", "model_calls": 0}
        probe("codex_read_only_sandbox", sandbox_probe)
    if online:
        probe("github_read_access", lambda: GitHub(runner, repo, 15).call("repo", "view", "Wuysau/AetherTrade", "--json", "nameWithOwner,defaultBranchRef"))
    def wsl_probe():
        w = c["wsl"]
        source = ("import json,os,shutil; p=" + repr(w["repo"]) + "; exe=shutil.which('opencode'); "
                  "print(json.dumps({'python':shutil.which('python3'),'git':shutil.which('git'),"
                  "'opencode':exe,'native_opencode':bool(exe and not os.path.realpath(exe).startswith('/mnt/')),"
                  "'clone_exists':os.path.isdir(p+'/.git')}))")
        result = runner.run(["wsl.exe", "--distribution", w["distribution"], "--exec", "python3", "-c", source], repo, 15)
        data = json.loads(checked(result))
        if not data["native_opencode"] or not data["clone_exists"]:
            raise Blocked("WSL native OpenCode/clone unavailable: " + json.dumps(data))
        return data
    probe("wsl_environment", wsl_probe)
    if checks[-1]["status"] == "BLOCKED" and c["opencode_environment"] != "wsl":
        checks[-1]["status"] = "UNAVAILABLE_OPTIONAL"
    try:
        approved = require_approval(c, Path(c["config_dir"]) / "approval.json")
        checks.append({"check": "real_execution_authorization", "status": "PASS",
                       "detail": {"approved_by": approved["approved_by"], "task_count": len(approved["authorized_tasks"]),
                                  "integration_target": c.get("integration_target", "origin-dev")}})
    except (Blocked, OSError, ValueError, KeyError) as exc:
        checks.append({"check": "real_execution_authorization", "status": "WAITING_HUMAN_APPROVAL", "detail": str(exc)})
    return {"mode": "READ_ONLY", "integration_target": c.get("integration_target", "origin-dev"), "local_dev": selected,
            "publication_mode": c["publication_mode"], "agent_calls": 0, "github_writes": 0, "checks": checks,
            "cached_origin_dev": local, "live_origin_dev": live,
            "configuration_sha256": approval_fingerprint(c), "usage": "UNKNOWN"}


def planning(c: dict):
    plan = Plan(Path(c["repo"]), c["pins"])
    validate_spec(Path(c["repo"]), c["manifest_sha256"])
    records = Store.snapshot(Path(c["state_dir"]))
    rows = []
    for tid, task in plan.tasks.items():
        rec = records.get(tid, {})
        state = rec.get("state", "PENDING")
        deps = [d for d in task.dependencies if records.get(d, {}).get("state") != "MERGED_VERIFIED"]
        if tid == "IP-34" and not rec:
            state, reason = "BLOCKED", "Existing bootstrap: recheck remote ancestry and original verification; never recreate"
        elif tid == "IP-00" and not rec:
            state, reason = "WAITING_PR", "Existing local implementation: merged remote PR + current review + integration evidence required"
        elif deps:
            reason = "Dependencies lack verified remote evidence: " + ", ".join(deps)
        else:
            reason = rec.get("reason", "Candidate only; live origin/dev and approval rechecked at execution")
        rows.append({"task_id": tid, "agent": task.agent, "state": state, "reason": reason,
                     "dependencies": list(task.dependencies), "verify_command": task.verify_command})
    return {"mode": "READ_ONLY", "publication_mode": c["publication_mode"], "tasks": rows, "task_count": len(rows), "dispatch_counts": {"Codex": 34, "OpenCode": 5},
            "plan_status": plan.plan_status, "agent_calls": 0, "github_writes": 0,
            "note": "Persisted completion is not fresh remote proof; execute revalidates every dependency."}

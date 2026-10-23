"""Self-contained Linux side of the WSL adapter. Receives JSON on stdin, never shell code."""
from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import select
from pathlib import Path


def native(value):
    p = Path(value)
    if not p.is_absolute() or not str(p).startswith("/home/") or ".." in p.parts:
        raise RuntimeError("Native /home/... paths required")
    if str(p.resolve()) != str(p):
        raise RuntimeError("WSL symlink paths forbidden")
    return p


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def git(repo, *args):
    r = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.quotePath=false", *args],
                       cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
    if r.returncode:
        raise RuntimeError("Git command failed: " + r.stderr.decode("utf-8", errors="replace")[-700:])
    return r.stdout.decode("utf-8").strip()


def clean(repo):
    if git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("Dirty WSL worktree")


def baseline(repo, p):
    raw = (repo / "docs/spec/spec-manifest.json").read_bytes()
    if sha256(raw) != p["manifest_sha256"]:
        raise RuntimeError("WSL manifest changed")
    files = json.loads(raw)["files"]
    if len(files) != 23:
        raise RuntimeError("Expected 23 approved files")
    for name, expected in {**files, **p["pins"]}.items():
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise RuntimeError("Unsafe pinned path")
        path = repo / name
        if path.is_symlink() or not path.resolve().is_relative_to(repo.resolve()) or sha256(path.read_bytes()) != expected:
            raise RuntimeError("WSL immutable input changed: " + name)


def proc_identity(pid):
    try:
        # comm can contain spaces; field 22 is index 19 after the closing parenthesis.
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def write_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def handle(p):
    repo, work, state = (native(p[x]) for x in ("repo", "worktree", "state"))
    tid = p["task_id"]
    if not re.fullmatch(r"IP-(0[0-9]|[12][0-9]|3[0-8])", tid):
        raise RuntimeError("Invalid task ID")
    if work == repo or work.is_relative_to(repo) or state.is_relative_to(repo) or state.is_relative_to(work):
        raise RuntimeError("Clone, worktree and state must be separate")
    action = p["action"]
    lease_path = state / "lease.json"
    if action == "stop":
        if lease_path.exists():
            lease = json.loads(lease_path.read_text())
            pid = lease.get("child_pid")
            if pid and lease.get("child_identity") == proc_identity(pid) and lease.get("task_id") == tid:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            bridge = lease.get("bridge_pid")
            if bridge and lease.get("bridge_identity") == proc_identity(bridge) and lease.get("task_id") == tid:
                try:
                    os.kill(bridge, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        return {"stopped": True}
    if action == "inspect" and not state.exists():
        raise RuntimeError("No registered WSL task state")
    if action not in {"inspect", "create", "stage"}:
        raise RuntimeError("Unknown bridge operation")
    state.mkdir(parents=True, exist_ok=True)
    with (state / "task.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if lease_path.exists():
            lease = json.loads(lease_path.read_text())
            if lease.get("child_pid") and lease.get("child_identity") == proc_identity(lease["child_pid"]):
                raise RuntimeError("Previous WSL Agent is still running; stop before recovery")
        if git(repo, "remote", "get-url", "origin") != p["remote_url"]:
            raise RuntimeError("WSL origin differs from approved remote")
        clean(repo)
        branch = "codex/autodispatch/" + tid.lower()
        if action == "create":
            if work.exists():
                raise RuntimeError("WSL worktree already exists")
            git(repo, "fetch", "--no-tags", "origin", "dev")
            if git(repo, "rev-parse", "origin/dev") != p["base_sha"]:
                raise RuntimeError("Windows/WSL origin/dev mismatch")
            work.parent.mkdir(parents=True, exist_ok=True)
            git(repo, "worktree", "add", "-b", branch, str(work), p["base_sha"])
            baseline(work, p)
            clean(work)
            return {"commit_sha": git(work, "rev-parse", "HEAD"), "clean": True}
        baseline(work, p)
        clean(work)
        before = git(work, "rev-parse", "HEAD")
        if action == "inspect":
            if before != p["expected_sha"]:
                raise RuntimeError("WSL recovery SHA mismatch")
            return {"commit_sha": before, "clean": True, "pid_alive": False}
        if git(work, "branch", "--show-current") != branch:
            raise RuntimeError("Unexpected WSL task branch")
        executable = native(p["opencode_command"][0])
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise RuntimeError("Native Linux OpenCode executable unavailable")
        prompt = state / "prompt.md"
        prompt.write_text(p["prompt"], encoding="utf-8")
        argv = [str(executable), "run", "--standalone", "--agent", "build", "--format", "json", "--file", str(prompt)]
        if p.get("model"):
            argv += ["--model", p["model"]]
        argv += ["--", "Perform only the attached task stage. Do not run tests or builds; do not commit or push."]
        env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "XDG_CONFIG_HOME"}}
        env.update(CI="1", NO_COLOR="1", GIT_TERMINAL_PROMPT="0")
        timeout = min(int(p["timeout"]), 3600)
        if timeout < 1:
            raise RuntimeError("Task budget exhausted")
        gate = "import os,sys,json; a=json.loads(sys.argv[1]); g=os.read(0,1); os.execvpe(a[0],a,os.environ) if g==b'G' else sys.exit(125)"
        child = subprocess.Popen([sys.executable, "-c", gate, json.dumps(argv)], cwd=work, env=env,
                                 stdin=subprocess.PIPE, start_new_session=True)
        # A sibling watchdog survives bridge SIGTERM/SIGKILL. EOF means bridge death;
        # the gate prevents launching the Agent before this supervision exists.
        watch_read, watch_write = os.pipe()
        watchdog = os.fork()
        if watchdog == 0:
            os.close(watch_write)
            for fd in (0, 1, 2):
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                select.select([watch_read], [], [], timeout + 1)
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            finally:
                os._exit(0)
        os.close(watch_read)
        write_json(lease_path, {"task_id": tid, "child_pid": child.pid, "child_identity": proc_identity(child.pid),
                                "bridge_pid": os.getpid(), "bridge_identity": proc_identity(os.getpid()), "expires_at": time.time() + timeout})
        try:
            child.stdin.write(b"G")
            child.stdin.close()
            code = child.wait(timeout=timeout)
        finally:
            os.close(watch_write)
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=10)
            os.waitpid(watchdog, 0)
            write_json(lease_path, {"task_id": tid, "child_pid": None, "bridge_pid": os.getpid(),
                                    "bridge_identity": proc_identity(os.getpid()), "released_at": time.time()})
        if code:
            raise RuntimeError(f"OpenCode exited {code}")
        if git(work, "rev-parse", "HEAD") != before or git(work, "branch", "--show-current") != branch:
            raise RuntimeError("OpenCode changed Git HEAD/branch")
        baseline(work, p)
        changed = set(git(work, "diff", "--name-only", "HEAD").splitlines())
        changed |= set(git(work, "ls-files", "--others", "--exclude-standard").splitlines())
        if not changed:
            raise RuntimeError("OpenCode made no task changes")
        for name in changed:
            if not any(fnmatch.fnmatchcase(name, s) for s in p["scope_write"]) or name.startswith("docs/spec/"):
                raise RuntimeError("WSL scope expansion: " + name)
            if p["stage"] == "red" and name not in {p["primary_test_file"], p["test_support_file"]}:
                raise RuntimeError("WSL RED changed a production file")
            target = work / name
            if target.is_symlink() or not target.resolve().is_relative_to(work):
                raise RuntimeError("Linked output forbidden")
            if target.is_file() and re.search(r"(?i)(?:sk-[a-z0-9_-]{16,}|gh[pousr]_[a-z0-9_]{16,}|(?:api[_-]?key|secret|password|access[_-]?token)[\"']?\s*[:=]\s*[\"'][^\"'\r\n]{8,}[\"'])", target.read_text(errors="replace")):
                raise RuntimeError("Potential secret in WSL output")
        if p["stage"] == "implement" and p.get("red_sha"):
            if git(work, "diff", "--name-only", p["red_sha"], "--", p["primary_test_file"], p["test_support_file"]):
                raise RuntimeError("WSL GREEN modified RED tests")
        git(work, "diff", "--check", "HEAD")
        git(work, "add", "--", *sorted(changed))
        git(work, "commit", "-m", f"feat({tid.lower()}): {p['stage']}")
        sha = git(work, "rev-parse", "HEAD")
        git(work, "diff", "--check", "origin/dev...HEAD")
        # One remote transfer per stage SHA. Never retry a failed/ambiguous push.
        push_receipt = state / f"push-{sha}.json"
        remote = git(work, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
        remote_sha = remote.split()[0] if remote else None
        if remote_sha != sha:
            if push_receipt.exists():
                raise RuntimeError("Previous push attempt already consumed; no retry")
            if remote_sha:
                git(work, "merge-base", "--is-ancestor", remote_sha, sha)
            write_json(push_receipt, {"sha": sha, "attempted_at": time.time()})
            git(work, "push", "origin", f"HEAD:refs/heads/{branch}")
        return {"commit_sha": sha, "writer_released": True, "clean": True, "branch": branch}


if __name__ == "__main__":
    payload = json.load(sys.stdin)
    try:
        result = handle(payload)
        print(json.dumps({"autodispatch_bridge": payload["nonce"], "result": result}), flush=True)
    except Exception as exc:
        print(json.dumps({"autodispatch_bridge": payload["nonce"], "error": str(exc)}), flush=True)
        raise SystemExit(2)

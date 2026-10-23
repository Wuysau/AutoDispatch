"""Run through WSL Python with only local Fake CLI + disposable native Git repositories."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import signal
from pathlib import Path

payload = json.load(sys.stdin)
ns = {"__name__": "bridge_under_test"}
exec(compile(payload["source"], "wsl_bridge.py", "exec"), ns)


def git(path, *args):
    return subprocess.check_output(["git", "-c", "core.autocrlf=false", *args], cwd=path, stderr=subprocess.STDOUT).decode().strip()


with tempfile.TemporaryDirectory(prefix="autodispatch-fake-", dir=Path.home()) as d:
    root = Path(d)
    seed, remote, clone = root / "seed", root / "remote.git", root / "clone"
    seed.mkdir()
    git(seed, "init", "-b", "dev")
    git(seed, "config", "user.name", "Offline Fake")
    git(seed, "config", "user.email", "offline@example.invalid")
    files = {}
    for i in range(23):
        name = f"docs/spec/approved/f{i}.txt"
        target = seed / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"approved {i}\n".encode())
        files[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest = seed / "docs/spec/spec-manifest.json"
    manifest.write_text(json.dumps({"files": files}))
    git(seed, "add", "--", "docs")
    git(seed, "commit", "-m", "offline fixture")
    base = git(seed, "rev-parse", "HEAD")
    git(root, "clone", "--bare", str(seed), str(remote))
    git(root, "clone", str(remote), str(clone))
    git(clone, "config", "user.name", "Offline Fake")
    git(clone, "config", "user.email", "offline@example.invalid")
    fake = root / "fake-opencode"
    fake.write_text("#!/usr/bin/python3\nimport pathlib,sys\n"
                    "prompt=pathlib.Path(sys.argv[sys.argv.index('--file')+1]).read_text()\n"
                    "pathlib.Path('tests').mkdir(exist_ok=True)\n"
                    "if prompt=='red':\n"
                    " pathlib.Path('tests/test_behavior.py').write_text(\"from pathlib import Path\\nns={}\\np=Path('implementation.py')\\nif p.exists(): exec(p.read_text(),ns)\\nassert ns.get('answer',lambda:None)()==42\\n\")\n"
                    "else:\n pathlib.Path('implementation.py').write_text('def answer():\\n return 42\\n')\n"
                    "print('SIMULATED OpenCode only')\n")
    fake.chmod(0o700)
    p = {"repo": str(clone), "worktree": str(root / "work"), "state": str(root / "state"), "task_id": "IP-05",
         "remote_url": str(remote), "base_sha": base, "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
         "pins": {}, "opencode_command": [str(fake)], "timeout": 10, "model": None,
         "scope_write": ["tests/test_behavior.py", "implementation.py"], "primary_test_file": "tests/test_behavior.py",
         "test_support_file": "tests/support.py"}
    ns["handle"](dict(p, action="create"))
    red = ns["handle"](dict(p, action="stage", stage="red", prompt="red"))
    verifier = root / "verifier"
    git(root, "clone", str(remote), str(verifier))
    git(verifier, "checkout", "--detach", red["commit_sha"])
    r = subprocess.run([sys.executable, "tests/test_behavior.py"], cwd=verifier, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert r.returncode == 1 and b"AssertionError" in r.stderr, r.stderr
    green = ns["handle"](dict(p, action="stage", stage="implement", prompt="green", red_sha=red["commit_sha"]))
    git(verifier, "fetch", "--no-tags", "origin", "refs/heads/codex/autodispatch/ip-05")
    git(verifier, "checkout", "--detach", green["commit_sha"])
    r = subprocess.run([sys.executable, "tests/test_behavior.py"], cwd=verifier, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert r.returncode == 0, r.stderr
    assert ns["handle"](dict(p, action="inspect", expected_sha=green["commit_sha"]))["clean"]
    assert git(verifier, "diff", "--name-only", red["commit_sha"], green["commit_sha"], "--", "tests/test_behavior.py") == ""
    # Killing the bridge must not leave an unbounded native Linux Agent behind.
    slow = root / "fake-slow-opencode"
    started, escaped = root / "started.txt", root / "escaped.txt"
    slow.write_text("#!/usr/bin/python3\nimport time,pathlib\npathlib.Path(" + repr(str(started)) + ").write_text('started')\ntime.sleep(1.5)\npathlib.Path(" + repr(str(escaped)) + ").write_text('escaped')\n")
    slow.chmod(0o700)
    p2 = dict(p, task_id="IP-20", worktree=str(root / "work2"), state=str(root / "state2"), opencode_command=[str(slow)])
    ns["handle"](dict(p2, action="create"))
    bridge = subprocess.Popen([sys.executable, "-c", payload["source"]], stdin=subprocess.PIPE,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    bridge.stdin.write(json.dumps(dict(p2, action="stage", stage="red", prompt="slow", nonce="watchdog-test")).encode())
    bridge.stdin.close()
    until = time.monotonic() + 10
    while not started.exists() and time.monotonic() < until:
        time.sleep(0.02)
    assert started.exists(), 'Fake child did not start'
    os.kill(bridge.pid, signal.SIGTERM)
    bridge.wait(timeout=5)
    time.sleep(1.7)
    assert not escaped.exists(), 'Native child outlived terminated bridge'
    print(json.dumps({"simulation": True, "real_agent_calls": 0, "red_exit": 1, "green_exit": 0,
                      "red_sha": red["commit_sha"], "green_sha": green["commit_sha"], "native_wsl_protocol": "PASS",
                      "bridge_death_stops_native_child": True}))

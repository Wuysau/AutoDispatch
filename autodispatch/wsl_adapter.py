from __future__ import annotations

import json
import uuid
from pathlib import Path, PurePosixPath

from .model import Blocked
from .process import checked
from .safety import wsl_argv, wsl_path


class WslAdapter:
    def __init__(self, backend):
        self.backend = backend
        self.c = backend.c
        self.w = self.c["wsl"]

    def call(self, task, work, action, prompt="", stage="", expected_sha=None):
        if action in {"create", "stage"} and self.c.get("integration_target") == "local-dev":
            raise Blocked("WSL remote handoff is disabled in no-push local-dev mode; select Windows OpenCode")
        nonce = uuid.uuid4().hex
        from .workers import default_profile, execution_config
        execution = execution_config(self.c, self.backend.store.records().get(task.id, {}), 'OpenCode')
        profile = default_profile(execution, 'OpenCode')
        model, effort = profile['model'], profile['reasoningEffort']
        root = wsl_path(self.w["worktree_root"])
        state = wsl_path(self.w["state_dir"])
        payload = {"nonce": nonce, "action": action, "task_id": task.id, "repo": wsl_path(self.w["repo"]),
                   "worktree": work.get("wsl_worktree", str(PurePosixPath(root) / task.id.lower())),
                   "state": str(PurePosixPath(state) / task.id), "remote_url": self.c["remote_url"],
                   "base_sha": work["base_sha"], "expected_sha": expected_sha,
                   "manifest_sha256": self.c["manifest_sha256"], "pins": self.c["pins"],
                   "opencode_command": self.w["opencode_command"], "prompt": prompt, "stage": stage,
                   "scope_write": list(task.scope_write), "primary_test_file": task.primary_test_file,
                   "test_support_file": task.test_support_file, "red_sha": work.get("red_sha"),
                   "model": model + (f'#{effort}' if effort else '') if model else None,
                   "timeout": self.backend.remaining(task) if action == "stage" else 45}
        source = Path(__file__).with_name("wsl_bridge.py").read_text(encoding="utf-8")
        argv = wsl_argv(self.w["distribution"], self.w["repo"], ["python3", "-c", source])
        runner = self.backend.runner if action == "stage" else self.backend.infra
        if action == "stop":
            from .process import Runner
            runner = Runner()
        result = runner.run(argv, self.backend.repo, payload["timeout"] + (100 if action == "stage" else 5),
                            tid=task.id, stage="wsl-" + action + ("-" + stage if stage else ""),
                            agent=action == "stage", stdin=json.dumps(payload), model=model, reasoning_effort=effort)
        checked(result)
        for line in reversed(result.output.splitlines()):
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("autodispatch_bridge") == nonce:
                if item.get("error"):
                    raise Blocked(item["error"])
                return item["result"]
        raise Blocked("WSL bridge did not return a matching machine receipt")

    def prepare(self, task, work):
        path = str(PurePosixPath(self.w["worktree_root"]) / task.id.lower())
        self.call(task, dict(work, wsl_worktree=path), "create")
        return path

    def stage(self, task, stage, work, prompt):
        result = self.call(task, work, "stage", prompt, stage)
        sha = result["commit_sha"]
        git = self.backend.git
        local = Path(work["worktree"])
        git.clean(local)
        expected = work.get("red_sha", work["base_sha"])
        if git.sha(local) != expected:
            raise Blocked("Windows handoff worktree moved unexpectedly")
        git.text(self.backend.repo, "fetch", "--no-tags", "origin", f"refs/heads/{work['branch']}")
        if git.sha(self.backend.repo, "FETCH_HEAD") != sha:
            raise Blocked("GitHub handoff SHA differs from WSL commit")
        git.text(local, "merge", "--ff-only", sha)
        self.backend.audit(task, work, sha)
        return sha

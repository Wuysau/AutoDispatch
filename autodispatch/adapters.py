from __future__ import annotations

import json
import os
import time
import uuid
import threading
from pathlib import Path

from .context import context
from .gitops import Git, GitHub
from .model import Blocked, Plan, PublicationPending, Task, digest, validate_spec, within
from .process import Runner, checked
from .safety import assert_review, check_scope, verification_argv, redact
from .verification import green, junit, vitest, receipt_valid
from .codex_policy import exec_argv


class RealBackend:
    """Windows adapters. Git/GitHub writes are reachable only from execute()."""
    def __init__(self, c, plan, store):
        self.c, self.plan, self.store = c, plan, store
        self.repo, self.root = Path(c["repo"]), Path(c["state_dir"])
        self.runner = Runner(self.root, store, c["max_agent_requests"])
        # Infrastructure commands don't overwrite the task's Agent PID/lease.
        self.infra = Runner(self.root)
        self.git = Git(self.infra, min(60, c["command_timeout_seconds"]))
        self.github = GitHub(self.infra, self.repo, min(60, c["command_timeout_seconds"]), c["required_checks"])
        self.base = None
        self.publication_deferred = c["publication_mode"] == "deferred"
        self.local_integration = c.get("integration_target") == "local-dev"
        self.integration_lock = threading.RLock()

    def current_base(self):
        with self.integration_lock:
            return self._current_base()

    def _current_base(self):
        if (self.root / "batch-active.json").exists():
            raise Blocked("Deferred-validation batch owns local integration; use its recorded entry point")
        if self.local_integration and (self.root / "local-merge-pending.json").exists():
            raise Blocked("Local merge has incomplete integration evidence; inspect saved merge state before continuing")
        if self.git.text(self.repo, "remote", "get-url", "origin") != self.c["remote_url"]:
            raise Blocked("Remote identity changed")
        self.git.clean(self.repo)
        if self.local_integration:
            if self.git.text(self.repo, "branch", "--show-current") != "dev":
                raise Blocked("Local integration checkout must be on dev")
            self.base = self.git.sha(self.repo, "dev")
        else:
            self.base = self.git.refresh(self.repo)
        return self.base

    def dependency_valid(self, record, base):
        sha = record.get("merge_sha", "")
        if record.get("state") != "MERGED_VERIFIED":
            return False
        if not self.local_integration and record.get("observed_dev_sha", record.get("verified_dev_sha")) != base:
            return False
        if self.local_integration and record.get("integration_target") != "local-dev":
            return False
        if not self.git.ancestor(self.repo, sha, base):
            return False
        if record.get("bootstrap"):
            return self.bootstrap_valid(base)
        return (record.get("review", {}).get("commit_sha") == record.get("commit_sha")
                and record.get("review", {}).get("status") == "APPROVED"
                and all(receipt_valid(r) for r in record.get("verification_receipts", []))
                and bool(record.get("verification_receipts")))

    def bootstrap_valid(self, base):
        code = "d5ed821a8c55376e2ea0be2fda0d72eaa9dd2f8e"
        if not self.git.ancestor(self.repo, code, base):
            return False
        # Read remote-ancestor blobs, never infer completion from a task card.
        for path, expected in [
            ("evidence/tasks/IP-34/green-remote.txt", "4 passed"),
            ("evidence/tasks/IP-34/review.md", "No publication-content blockers"),
            ("docs/worklog/IP-34.md", "IP-34 complete"),
        ]:
            if expected not in self.git.text(self.repo, "show", f"{base}:{path}"):
                return False
        if self.local_integration:
            return True
        metadata = self.github.call("repo", "view", "Wuysau/AetherTrade", "--json", "nameWithOwner,visibility,defaultBranchRef")
        return (metadata["nameWithOwner"] == "Wuysau/AetherTrade" and metadata["visibility"] == "PUBLIC"
                and metadata["defaultBranchRef"]["name"] == "main")

    def prepare(self, task, base):
        if task.id in {"IP-34", "IP-00"}:
            raise Blocked("Existing tasks must be adopted, never recreated")
        if self.local_integration and task.agent == "OpenCode" and self.c["opencode_environment"] == "wsl":
            raise Blocked("Local-dev with WSL requires a no-push SHA transport; use Windows OpenCode for local integration")
        root = Path(self.c["worktree_root"])
        root.mkdir(parents=True, exist_ok=True)
        path = root / task.id.lower()
        branch = "codex/autodispatch/" + task.id.lower()
        self.store.update(task.id, worktree=str(path), branch=branch, base_sha=base, executor=task.agent)
        self.git.create(self.repo, path, base, branch)
        validate_spec(path, self.c["manifest_sha256"])
        Plan(path, self.c["pins"])
        work = {"worktree": str(path), "branch": branch, "base_sha": base}
        if task.agent == "OpenCode" and self.c["opencode_environment"] == "wsl":
            from .wsl_adapter import WslAdapter
            work["wsl_worktree"] = WslAdapter(self).prepare(task, work)
        return work

    def run_agent(self, task, stage, work, evidence=None):
        from .workers import default_profile, execution_config
        execution = execution_config(self.c, self.store.records().get(task.id, {}), task.agent)
        path = Path(work["worktree"])
        if stage == "batch-repair":
            initial = set(self.git.text(path, "diff", "--name-only", "HEAD").splitlines())
            initial |= set(self.git.text(path, "ls-files", "--others", "--exclude-standard").splitlines())
            if initial:
                check_scope(task, sorted(initial), path)
            validate_spec(path, self.c["manifest_sha256"])
        else:
            self.git.clean(path)
        before = self.git.sha(path)
        prompt = self.agent_context(task, stage, before, evidence, path)
        if task.agent == "OpenCode" and self.c["opencode_environment"] == "wsl":
            from .wsl_adapter import WslAdapter
            return WslAdapter(self).stage(task, stage, work, prompt)
        if task.agent == "Codex":
            output = self.root / "outputs" / f"{task.id}-{stage}-{uuid.uuid4().hex}.txt"
            output.parent.mkdir(parents=True, exist_ok=True)
            argv = exec_argv(execution, path, "workspace-write", output)
            stdin = prompt
        else:
            prompt_file = self.root / "prompts" / f"{task.id}-{stage}-{uuid.uuid4().hex}.md"
            prompt_file.parent.mkdir(parents=True, exist_ok=True)
            prompt_file.write_text(prompt, encoding="utf-8")
            argv = [*self.c["opencode_command"], "run", "--standalone", "--agent", "build", "--format", "json",
                    "--file", str(prompt_file), "--", "Perform only the attached authorized task stage."]
            stdin = None
        profile = default_profile(execution, task.agent)
        selected_model, effort = profile['model'], profile['reasoningEffort']
        if selected_model:
            if task.agent == "OpenCode":
                index = argv.index("--")
                argv[index:index] = ["--model", selected_model + (f'#{effort}' if effort else '')]
        result = self.runner.run(argv, path, self.remaining(task), tid=task.id, stage=stage, agent=True, stdin=stdin,
                                 model=selected_model, reasoning_effort=effort)
        if task.agent == "Codex" and output.exists():
            output.write_text(redact(output.read_text(encoding="utf-8")), encoding="utf-8")
        checked(result)
        if stage in {"batch-implement", "batch-repair"}:
            if task.agent == "Codex":
                message = output.read_text(encoding="utf-8") if output.exists() else ""
            else:
                messages = []
                for line in result.output.splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and event.get("type") == "text":
                        messages.append(event.get("part", {}).get("text", ""))
                message = messages[-1] if messages else ""
            if not message.lstrip().startswith("CODE_READY_VERIFY_PENDING"):
                raise Blocked("Agent did not return CODE_READY_VERIFY_PENDING: " + message[:1200])
        if self.git.sha(path) != before:
            raise Blocked("Agent changed HEAD; runner owns commit creation")
        if self.git.text(path, "branch", "--show-current") != work["branch"]:
            raise Blocked("Agent changed branch")
        validate_spec(path, self.c["manifest_sha256"])
        changed = set(self.git.text(path, "diff", "--name-only", "HEAD").splitlines())
        changed |= set(self.git.text(path, "ls-files", "--others", "--exclude-standard").splitlines())
        check_scope(task, sorted(changed), path, red=stage == "red")
        checked(self.git.run(path, "diff", "--check", "HEAD"))
        self.git.text(path, "add", "--", *sorted(changed))
        self.git.text(path, "commit", "-m", f"feat({task.id.lower()}): {stage} {task.title}")
        sha = self.git.sha(path)
        self.audit(task, work, sha)
        if stage == "implement" and work.get("red_sha"):
            tests = [task.primary_test_file, task.test_support_file]
            tests = [x for x in tests if x]
            if self.git.text(path, "diff", "--name-only", work["red_sha"], sha, "--", *tests):
                raise Blocked("GREEN changed the RED tests; new RED verification required")
        return sha

    def agent_context(self, task, stage, sha, evidence, path):
        return context(self.plan, task, stage, sha, evidence, repo=path)

    def remaining(self, task):
        deadline = self.store.records()[task.id].get("deadline", time.time() + self.c["task_timeout_seconds"])
        remaining = int(deadline - time.time())
        if remaining < 1:
            raise Blocked("Per-task wall-clock budget exhausted")
        return min(self.c["command_timeout_seconds"], remaining)

    def audit(self, task, work, sha):
        path = Path(work["worktree"])
        self.git.clean(path)
        if self.git.sha(path) != sha:
            raise Blocked("Commit moved during stage")
        if not self.git.ancestor(path, work["base_sha"], sha) or sha == work["base_sha"]:
            raise Blocked("Task has no new descendant commit")
        validate_spec(path, self.c["manifest_sha256"])
        check_scope(task, self.git.changed(path, work["base_sha"]), path)
        # The selected integration base is recorded explicitly; no remote success claim in local mode.
        ref = work["base_sha"] if self.local_integration else "origin/dev"
        checked(self.git.run(path, "diff", "--check", f"{ref}...HEAD"))

    def integrate_local(self, task, work, sha, evidence, review):
        if not self.local_integration:
            raise Blocked("Local merge was not authorized by configuration")
        assert_review(review, task, sha)
        with self.integration_lock:
            self.audit(task, work, sha)
            before = self.current_base()
            from .control import atomic_json
            marker = self.root / "local-merge-pending.json"
            atomic_json(marker, {"task_id": task.id, "before_sha": before, "reviewed_sha": sha, "branch": work["branch"]})
            self.store.update(task.id, local_merge_intent=True, local_dev_before=before)
            self.git.text(self.repo, "merge", "--no-ff", "--no-edit", work["branch"])
            merged = self.git.sha(self.repo)
            if not self.git.ancestor(self.repo, sha, merged):
                raise Blocked("Merged local dev does not contain reviewed task commit")
            self.store.update(task.id, local_merge_sha=merged, local_merge_intent=False)
            integration = self.verify(task, merged, "green")
            green(integration)
            validate_spec(self.repo, self.c["manifest_sha256"])
            marker.unlink()
            return {"merge_sha": merged, "integration": integration}

    def validation_tree(self, task, sha):
        root = Path(self.c["worktree_root"])
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{task.id.lower()}-verify-{uuid.uuid4().hex[:10]}"
        self.git.create(self.repo, path, sha)
        self.store.event(task.id, validation_worktree=str(path), validation_sha=sha)
        return path

    def environment(self, task, path):
        python = path / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        # Environments are local to each verification worktree; never borrowed or copied.
        import sys
        for argv in ([sys.executable, "-m", "venv", ".venv"],
                     [str(python), "-m", "pip", "install", "-e", ".[test]"]):
            checked(self.runner.run(argv, path, self.remaining(task), tid=task.id, stage="environment"))
        if task.verify_command.startswith("npm"):
            checked(self.runner.run([*self.c["npm_command"], "--prefix", "web", "ci"], path,
                                    self.remaining(task), tid=task.id, stage="npm-ci"))
        return str(python)

    def verify(self, task, sha, stage):
        path = self.validation_tree(task, sha)
        validate_spec(path, self.c["manifest_sha256"])
        python = self.environment(task, path)
        report = self.root / "reports" / f"{task.id}-{stage}-{uuid.uuid4().hex}"
        report.parent.mkdir(parents=True, exist_ok=True)
        argv = verification_argv(task.verify_command, python, self.c["npm_command"])
        is_pytest = task.verify_command.startswith("python")
        report = report.with_suffix(".xml" if is_pytest else ".json")
        argv += [f"--junitxml={report}"] if is_pytest else ["--reporter=json", f"--outputFile={report}"]
        result = self.runner.run(argv, path, self.remaining(task), tid=task.id, stage=f"machine-{stage}")
        if not report.is_file():
            raise Blocked("Test process produced no machine report; not a valid RED/GREEN")
        stats = junit(report) if is_pytest else vitest(report)
        self.git.clean(path)
        if self.git.sha(path) != sha:
            raise Blocked("Verification changed HEAD")
        validate_spec(path, self.c["manifest_sha256"])
        evidence = {"commit_sha": sha, "exit_code": result.code, "stats": stats, "receipt": result.receipt,
                    "report": str(report), "report_sha256": digest(report.read_bytes()), "cwd": str(path)}
        if stage == "green":
            green(evidence)
            evidence["regressions"] = self.regressions(task, path, python)
        # New Codex process examines actual test code and machine proof for this exact SHA.
        evidence["codex_verification"] = self.review(task, sha, path, evidence, f"{stage}-check")
        return evidence

    def regressions(self, task, path, python):
        # Whole Python suite plus immutable-spec checks are mandatory.
        commands = [[python, "tools/check_spec_baseline.py"],
                    [python, "docs/spec/approved/trading-architecture/evidence/validate_contracts.py"],
                    [python, "-m", "pytest", "-v"]]
        for command in self.c["regressions"].get(task.id, []):
            commands.append(verification_argv(command, python, self.c["npm_command"]))
        if task.verify_command.startswith("npm"):
            commands.append([*self.c["npm_command"], "--prefix", "web", "run", "typecheck"])
        receipts = []
        for argv in commands:
            result = self.runner.run(argv, path, self.remaining(task), tid=task.id, stage="regression")
            checked(result)
            receipts.append(result.receipt)
        self.git.clean(path)
        return receipts

    def review(self, task, sha, path, evidence, phase="review"):
        from .workers import execution_config
        execution = execution_config(self.c, self.store.records().get(task.id, {}), 'Codex')
        folder = self.root / "reviews"
        folder.mkdir(parents=True, exist_ok=True)
        output = folder / f"{task.id}-{phase}-{uuid.uuid4().hex}.json"
        schema = Path(__file__).resolve().parent.parent / "schemas" / "review.schema.json"
        argv = exec_argv(execution, path, "read-only", output, schema)
        result = self.runner.run(argv, path, self.remaining(task), tid=task.id, stage=phase, agent=True,
                                 stdin=context(self.plan, task, phase, sha, evidence, repo=path), model=execution.get("codex_model"),
                                 reasoning_effort=execution.get('codex_reasoning_effort'))
        if output.exists():
            output.write_text(redact(output.read_text(encoding="utf-8")), encoding="utf-8")
        checked(result)
        report = json.loads(output.read_text(encoding="utf-8"))
        assert_review(report, task, sha)
        self.git.clean(path)
        if self.git.sha(path) != sha:
            raise Blocked("Read-only review moved HEAD")
        validate_spec(path, self.c["manifest_sha256"])
        self.store.event(task.id, review_receipt=result.receipt, review_report=report)
        return report

    def independent_review(self, task, sha, evidence):
        path = self.validation_tree(task, sha)
        return self.review(task, sha, path, evidence)

    def publish(self, task, work, sha, evidence, review):
        if self.publication_deferred:
            raise Blocked("GitHub pushes and PR creation deferred by user")
        self.audit(task, work, sha)
        branch = work["branch"]
        if branch != "codex/autodispatch/" + task.id.lower():
            raise Blocked("Only task branches can be pushed")
        # Record intent before every remote write. Interrupted writes require reconciliation.
        self.store.update(task.id, "WAITING_PR", publish_intent=True)
        remote = self.git.text(Path(work["worktree"]), "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
        if remote and remote.split()[0] != sha:
            raise Blocked("Remote task branch differs from verified SHA; never overwrite")
        if not remote:
            rec = self.store.records()[task.id]
            if rec.get("publication_attempted_sha") == sha:
                raise PublicationPending("Task push attempt already consumed; no repeated push")
            self.store.update(task.id, publication_attempted_sha=sha)
            try:
                self.git.text(Path(work["worktree"]), "push", "origin", f"HEAD:refs/heads/{branch}")
            except (Blocked, OSError) as exc:
                raise PublicationPending("Single task-branch push failed; local verified commit retained: " + str(exc)) from exc
        body = self.root / "reports" / f"{task.id}-pr.md"
        changed = self.git.changed(Path(work["worktree"]), work["base_sha"])
        body.write_text(f"{task.id}: {task.title}\n\nCommit: {sha}\n\n"
                        + "Changed paths:\n" + "\n".join(f"- `{p}`" for p in changed)
                        + "\n\nMachine evidence (external logs retained locally):\n```json\n"
                        + json.dumps(evidence, ensure_ascii=False, indent=2) + "\n```\n\nIndependent review:\n```json\n"
                        + json.dumps(review, ensure_ascii=False, indent=2)
                        + "\n```\n\nNOT_RUN: T01-T66, native qualification, venue/live tests unless separately registered.\n"
                          "Human merge approval required. No automatic merge.\n", encoding="utf-8")
        pr = self.github.draft(branch, f"{task.id}: {task.title}", body)
        if not pr or pr["headRefOid"] != sha or pr["baseRefName"] != "dev":
            raise Blocked("PR does not match tested commit/base")
        return pr

    def inspect_work(self, record):
        path = Path(record["worktree"])
        self.git.clean(path)
        sha = self.git.sha(path)
        if record.get("commit_sha") and sha != record["commit_sha"]:
            raise Blocked("Worktree SHA differs from journal")
        validate_spec(path, self.c["manifest_sha256"])
        task = self.plan.tasks[record["task_id"]]
        check_scope(task, self.git.changed(path, record["base_sha"]), path)
        if self.git.text(path, "branch", "--show-current") != record["branch"]:
            raise Blocked("Worktree branch differs from journal")
        if record.get("wsl_worktree"):
            from .wsl_adapter import WslAdapter
            WslAdapter(self).call(task, record, "inspect", expected_sha=sha)
        return {"sha": sha, "clean": True}

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import uuid
from pathlib import Path

from .engine import Engine
from .gitops import GitHub
from .model import Blocked, Plan, digest
from .process import Runner, checked
from .safety import assert_review
from .store import CoordinatorLock, Store
from .verification import junit, receipt_valid


class FakeGit:
    def __init__(self, root):
        self.root = root
        self.commits = {}
        self.base = "a" * 40

    def commit(self, path):
        files = {p.name: p.read_text(encoding="utf-8") for p in path.glob("*.py")}
        sha = hashlib.sha1(json.dumps(files, sort_keys=True).encode()).hexdigest()
        self.commits[sha] = files
        folder = self.root / "fake-git"
        folder.mkdir(exist_ok=True)
        (folder / f"{sha}.json").write_text(json.dumps(files), encoding="utf-8")
        return sha

    def checkout(self, sha, path):
        files = json.loads((self.root / "fake-git" / f"{sha}.json").read_text(encoding="utf-8"))
        path.mkdir(parents=True)
        for name, value in files.items():
            (path / name).write_text(value, encoding="utf-8")

    def ancestor(self, repo, older, newer):
        return newer == self.base and (self.root / "fake-git" / f"{older}.json").exists()


class FakeGitHub:
    ci_passed = staticmethod(GitHub.ci_passed)

    def __init__(self, required=None, ci="SUCCESS"):
        self.checks = ["offline"] if required is None else required
        self.ci = ci
        self.prs = {}
        self.created = 0
        self.merges = 0

    def required(self):
        return self.checks

    def pr(self, branch):
        return self.prs.get(branch)


class FakeBackend:
    def __init__(self, root, plan, store, faults=None, max_requests=100, required=None, ci="SUCCESS"):
        self.root, self.plan, self.store = root, plan, store
        self.runner = Runner(root, store, max_requests)
        self.git = FakeGit(root)
        self.github = FakeGitHub(required, ci)
        self.faults = faults or {}
        self.calls = []
        self.publication_deferred = False

    def current_base(self):
        return self.git.base

    def dependency_valid(self, record, base):
        return (record.get("state") == "MERGED_VERIFIED" and record.get("verified_dev_sha") == base
                and record.get("simulation") is True and record.get("merge_sha") == base)

    def prepare(self, task, base):
        path = self.root / "worktrees" / task.id
        if path.exists():
            raise Blocked("Fake worktree conflict")
        path.mkdir(parents=True)
        return {"worktree": str(path), "branch": "codex/autodispatch/" + task.id.lower(), "base_sha": base}

    def cli(self, task, stage, path, sha="", report=None, agent=True):
        self.calls.append((task.id, stage))
        payload = {"task_id": task.id, "sha": sha, "fault": self.faults.get((task.id, stage)), "report": str(report)}
        argv = [sys.executable, str(Path(__file__).with_name("fake_cli.py")), stage, json.dumps(payload)]
        return self.runner.run(argv, path, 15, tid=task.id, stage=stage, agent=agent)

    def run_agent(self, task, stage, work, evidence=None):
        path = Path(work["worktree"])
        checked(self.cli(task, stage, path))
        return self.git.commit(path)

    def verify(self, task, sha, stage):
        path = self.root / "verification" / f"{task.id}-{uuid.uuid4().hex}"
        self.git.checkout(sha, path)
        report = path.parent / f"{path.name}.xml"
        result = self.cli(task, "test", path, sha, report, agent=False)
        review_result = self.cli(task, f"{stage}-check", path, sha)
        review = json.loads(checked(review_result))
        assert_review(review, task, sha)
        return {"commit_sha": sha, "exit_code": result.code, "stats": junit(report),
                "receipt": result.receipt, "report_sha256": digest(report.read_bytes()),
                "report": str(report), "codex_verification": review, "regressions": [], "simulation": True}

    def independent_review(self, task, sha, evidence):
        path = self.root / "reviews" / uuid.uuid4().hex
        self.git.checkout(sha, path)
        return json.loads(checked(self.cli(task, "review", path, sha)))

    def publish(self, task, work, sha, evidence, review):
        if self.publication_deferred:
            raise Blocked("Publication deferred")
        existing = self.github.pr(work["branch"])
        if existing:
            return existing
        if self.faults.get((task.id, "publish")):
            raise Blocked("Simulated authentication/network failure")
        self.github.created += 1
        pr = {"number": self.github.created, "url": "fake://pr/" + task.id, "state": "OPEN", "isDraft": True,
              "headRefOid": sha, "baseRefName": "dev", "statusCheckRollup": [{"name": "offline", "conclusion": self.github.ci}]}
        self.github.prs[work["branch"]] = pr
        return pr

    def inspect_work(self, rec):
        path = Path(rec["worktree"])
        if not path.exists():
            raise Blocked("Missing worktree")
        sha = self.git.commit(path)
        if sha != rec.get("commit_sha"):
            raise Blocked("Unexpected dirty fake tree")
        return {"sha": sha, "clean": True}

    def human_merge(self, tid):
        rec = self.store.records()[tid]
        task = self.plan.tasks[tid]
        pr = self.github.pr(rec["branch"])
        assert_review(rec["review"], task, rec["commit_sha"])
        if not self.github.ci_passed(pr, self.github.required()) or not receipt_valid(rec["green"]["receipt"]):
            raise Blocked("Fake merge lacks CI or machine evidence")
        if rec["green"]["exit_code"] or rec["green"]["commit_sha"] != rec["commit_sha"]:
            raise Blocked("Fake merge SHA/test mismatch")
        if not (self.root / "fake-git" / f"{rec['commit_sha']}.json").exists():
            raise Blocked("Fake merge missing commit")
        pr["state"] = "MERGED"
        pr["mergeCommit"] = {"oid": self.git.base}
        self.github.merges += 1
        self.store.update(tid, "MERGED_VERIFIED", simulation=True, merge_sha=self.git.base, verified_dev_sha=self.git.base, pr=pr)


def seed_dependencies(plan, store, tasks, base):
    seeded = set()
    targets = set(tasks)

    def seed(tid):
        for dep in plan.tasks[tid].dependencies:
            seed(dep)
            if dep not in targets:
                seeded.add(dep)
                store.update(dep, "MERGED_VERIFIED", simulation=True, merge_sha=base, verified_dev_sha=base)
    for tid in tasks:
        seed(tid)
    return sorted(seeded)


def demo(c, output):
    output = output.resolve()
    if output.exists():
        raise Blocked("Demo output must be a new directory; never overwrite evidence")
    if output.is_relative_to(Path(c["repo"])):
        raise Blocked("Demo must be outside product checkout")
    output.mkdir(parents=True)
    plan = Plan(Path(c["repo"]), c["pins"])
    with CoordinatorLock(output):
        store = Store(output)
        try:
            backend = FakeBackend(output, plan, store)
            task_ids = ["IP-35", "IP-05", "IP-20", "IP-28", "IP-29", "IP-31"]
            seeded = seed_dependencies(plan, store, task_ids, backend.current_base())
            # Seeding transitive prerequisites can include another demo target. Reset targets explicitly.
            for tid in task_ids:
                store.update(tid, "PENDING", simulation=True)
            engine = Engine(plan, store, backend, task_ids)
            rounds = []
            for _ in range(6):
                result = engine.run(max_workers=c['max_workers'], max_tasks=6)
                rounds.append(result["launched_tasks"])
                for tid in task_ids:
                    if store.records()[tid]["state"] == "WAITING_HUMAN_APPROVAL":
                        backend.human_merge(tid)
                if all(store.records()[tid]["state"] == "MERGED_VERIFIED" for tid in task_ids):
                    break
            result = {"simulation": True, "real_agent_calls": 0, "real_github_writes": 0,
                      "seeded_simulated_dependencies": seeded, "round_launch_counts": rounds,
                      "tasks": {tid: {"state": store.records()[tid]["state"],
                                      "states": list(dict.fromkeys(x["state"] for x in store.history(tid) if "state" in x))}
                                for tid in task_ids}, "fake_cli_calls": len(backend.calls)}
            (output / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            return result
        finally:
            store.close()

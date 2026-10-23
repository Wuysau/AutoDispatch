"""Explicitly authorized local integration first, machine validation last.

This is an opt-in development workflow. It never promotes an untested merge to
MERGED_VERIFIED, changes trading qualification, or publishes anything remotely.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from .adapters import RealBackend
from .control import acknowledge_stop, atomic_json
from .engine import Engine
from .handoff import service_lease
from .model import Blocked, Plan, digest, load_config, validate_spec
from .process import checked
from .safety import approval_fingerprint, conflicts, require_approval, verification_argv
from .store import CoordinatorLock, Store, process_alive
from .verification import green, junit, vitest
from .workers import worker_capacity

INTEGRATED = "INTEGRATED_UNVERIFIED"
REASON = "已集成本地 dev，等待批次统一校验；不代表验证通过"


def batch_approval(c, path, approved):
    obj = json.loads(path.read_text(encoding="utf-8"))
    if (obj.get("configuration_sha256") != approval_fingerprint(c)
            or obj.get("workflow") != "integrate-first-validate-last"
            or obj.get("allow_unverified_local_integration") is not True
            or not obj.get("user_request") or not obj.get("approved_at")):
        raise Blocked("Explicit dated batch workflow authorization must match configuration")
    if c.get("integration_target") != "local-dev" or c.get("publication_mode") != "deferred":
        raise Blocked("Batch workflow permits local dev only, with publication deferred")
    if c.get("opencode_environment") != "windows":
        raise Blocked("Batch workflow requires isolated Windows worktrees, no remote SHA transport")
    if set(obj.get("authorized_tasks", [])) != set(approved["authorized_tasks"]):
        raise Blocked("Batch task authorization differs from existing execution approval")
    return obj


class BatchBackend(RealBackend):
    def _current_base(self):
        if (self.root / "local-merge-pending.json").exists():
            raise Blocked("Interrupted merge; inspect before continuing")
        if self.git.text(self.repo, "remote", "get-url", "origin") != self.c["remote_url"]:
            raise Blocked("Remote identity changed")
        self.git.clean(self.repo)
        if self.git.text(self.repo, "branch", "--show-current") != "dev":
            raise Blocked("Batch integration checkout must be local dev")
        return self.git.sha(self.repo, "dev")

    def dependency_valid(self, record, base):
        if record.get("state") == INTEGRATED:
            return (record.get("batch_id") == self.batch_id
                    and self.git.ancestor(self.repo, record.get("merge_sha", ""), base))
        return super().dependency_valid(record, base)

    def prepare(self, task, base):
        record = self.store.records().get(task.id, {})
        path = Path(record.get("worktree", ""))
        if record.get("worktree") and path.is_dir():
            if process_alive(record.get("pid")) or record.get("pending_launch"):
                raise Blocked("An existing worker still owns this task")
            self.git.clean(path)
            if self.git.text(path, "branch", "--show-current") != record.get("branch"):
                raise Blocked("Existing task branch changed")
            if record.get("commit_sha") and self.git.sha(path) != record["commit_sha"]:
                raise Blocked("Existing task commit changed")
            # Preserve prior commits and synchronize current dependency code. No reset/rebase.
            self.git.text(path, "merge", "--no-edit", base)
            validate_spec(path, self.c["manifest_sha256"])
            return {"worktree": str(path), "branch": record["branch"], "base_sha": base}
        return super().prepare(task, base)

    def agent_context(self, task, stage, sha, evidence, path):
        records = self.store.records()
        receipts = {dep: {k: records[dep].get(k) for k in
                         ("state", "commit_sha", "merge_sha", "integration_target", "batch_id")}
                    for dep in task.dependencies}
        return super().agent_context(task, stage, sha, {
            "workflow": "integrate-first-validate-last",
            "user_request": self.request["user_request"],
            "dependency_integration_receipts": receipts,
            "warning": "Integrated does not mean verified. All task tests/review are deferred.",
        }, path)

    def integrate_unverified(self, task, work, sha):
        with self.integration_lock:
            before = self.current_base()
            self.audit(task, work, sha)
            marker = self.root / "local-merge-pending.json"
            atomic_json(marker, {"task_id": task.id, "before_sha": before,
                                 "implementation_sha": sha, "batch_id": self.batch_id,
                                 "verification": "DEFERRED"})
            self.store.update(task.id, local_merge_intent=True, local_dev_before=before)
            self.git.text(self.repo, "merge", "--no-ff", "--no-edit", work["branch"])
            merged = self.git.sha(self.repo)
            if not self.git.ancestor(self.repo, sha, merged):
                raise Blocked("Local dev does not contain the implementation commit")
            validate_spec(self.repo, self.c["manifest_sha256"])
            self.store.update(task.id, INTEGRATED, merge_sha=merged, commit_sha=sha,
                              local_merge_intent=False, batch_id=self.batch_id,
                              integration_target="local-dev", local_only=True,
                              verification="DEFERRED_TO_BATCH", reason=REASON,
                              writer_released=True, pid=None)
            marker.unlink()
            return merged


class BatchEngine(Engine):
    def implementation_stage(self, task):
        return "batch-implement"

    def retry_count(self, task):
        return 0

    def ready(self, base):
        records, ready = self.store.records(), []
        for task in self.plan.tasks.values():
            if task.id not in self.authorization or task.id in {"IP-00", "IP-34"}:
                continue
            if records.get(task.id, {}).get("state", "PENDING") not in {"PENDING", "READY"}:
                continue
            if not all(self.backend.dependency_valid(records.get(d, {}), base) for d in task.dependencies):
                continue
            holders = [self.plan.tasks[tid] for tid, rec in records.items()
                       if tid in self.plan.tasks and tid != task.id and rec.get("worktree")
                       and rec.get("state") not in {INTEGRATED, "MERGED_VERIFIED"}]
            if not any(conflicts(task, held) for held in holders + ready):
                ready.append(task)
        return ready

    def task(self, task, base):
        self.store.update(task.id, "RUNNING", executor=task.agent, coordinator_pid=os.getpid(),
                          deadline=time.time() + self.timeout, automatic_retries=self.retry_count(task),
                          reason="批量实现：测试与审查延后至全部集成后")
        try:
            work = self.backend.prepare(task, base)
            self.store.update(task.id, **work)
            sha = self.backend.run_agent(task, self.implementation_stage(task), work)
            self.store.update(task.id, "CODE_READY_VERIFY_PENDING", commit_sha=sha, writer_released=True)
            self.backend.integrate_unverified(task, work, sha)
        except Exception as exc:
            self.store.update(task.id, "BLOCKED", reason=f"{type(exc).__name__}: {exc}",
                              pid=None, pending_launch=False, writer_released=True)


def final_checks(backend, plan, authorized):
    """Execute every target and broad regression at ONE immutable final SHA.

    Never stop after the first failure, and never call an empty/skipped suite GREEN.
    Native platform qualification remains separate from these offline checks.
    """
    sha = backend.current_base()
    task = plan.tasks["IP-01"]
    path = backend.validation_tree(task, sha)
    folder = backend.root / "reports" / f"batch-{backend.batch_id}"
    folder.mkdir(parents=True, exist_ok=True)
    result = {"commit_sha": sha, "worktree": str(path), "status": "RUNNING", "checks": [],
              "qualification": "Unrun native/trading scenarios remain NOT_RUN; no LIVE authorization",
              "independent_review": "NOT_RUN", "finished_at": None}
    report = folder / "summary.json"
    atomic_json(report, result)

    def run(name, argv, *, target=None):
        try:
            process = backend.runner.run(argv, path, backend.c["command_timeout_seconds"],
                                         tid="BATCH", stage="final-" + name)
            item = {"name": name, "exit_code": process.code, "receipt": process.receipt,
                    "status": "PASS" if process.code == 0 else "FAIL"}
            if target:
                parsed = junit(target) if target.suffix == ".xml" else vitest(target)
                item["stats"] = parsed
                try:
                    green({"exit_code": process.code, "stats": parsed})
                except Blocked:
                    item["status"] = "FAIL"
            return item
        except (Blocked, OSError, ValueError, ET.ParseError) as exc:
            return {"name": name, "status": "FAIL", "error": str(exc)}

    import sys
    python = path / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    setup = [run("venv", [sys.executable, "-m", "venv", ".venv"])]
    if setup[0]["status"] == "PASS":
        setup.append(run("dependencies", [str(python), "-m", "pip", "install", "-e", ".[test]"]))
    result["checks"].extend(setup)
    if all(x["status"] == "PASS" for x in setup):
        npm_setup = run("npm-ci", [*backend.c["npm_command"], "--prefix", "web", "ci"])
        result["checks"].append(npm_setup)
        for tid in sorted(authorized):
            current = plan.tasks[tid]
            argv = verification_argv(current.verify_command, str(python), backend.c["npm_command"])
            is_python = current.verify_command.startswith("python")
            target = folder / (tid + (".xml" if is_python else ".json"))
            argv += ([f"--junitxml={target}"] if is_python else
                     ["--reporter=json", f"--outputFile={target}"])
            result["checks"].append(run(tid, argv, target=target))
            atomic_json(report, result)
        broad = [
            ("spec-baseline", [str(python), "tools/check_spec_baseline.py"]),
            ("approved-contracts", [str(python), "docs/spec/approved/trading-architecture/evidence/validate_contracts.py"]),
            ("plan-snapshot", [str(python), "evidence/tasks/IP-00/check_plan_snapshot.py"]),
            ("web-typecheck", [*backend.c["npm_command"], "--prefix", "web", "run", "typecheck"]),
        ]
        full = folder / "full-pytest.xml"
        result["checks"].append(run("full-pytest", [str(python), "-m", "pytest", "-v", f"--junitxml={full}"], target=full))
        for name, argv in broad:
            result["checks"].append(run(name, argv))
    try:
        validate_spec(path, backend.c["manifest_sha256"])
        backend.git.clean(path)
        if backend.current_base() != sha:
            raise Blocked("Local dev moved during final validation")
    except Blocked as exc:
        result["checks"].append({"name": "final-integrity", "status": "FAIL", "error": str(exc)})
    result.update(status="CHECKS_PASSED" if all(x["status"] == "PASS" for x in result["checks"]) else "FAILED",
                  finished_at=time.time())
    atomic_json(report, result)
    return report, result


def run_batch(config_path, approval_path, request_path):
    c = load_config(config_path)
    approved = require_approval(c, approval_path)
    request = batch_approval(c, request_path, approved)
    plan = Plan(Path(c["repo"]), c["pins"])
    root = Path(c["state_dir"])
    # A persisted stop must be acknowledged explicitly by this new operator request.
    if (root / "stop.request").exists():
        if process_alive(json.loads((root / "service.json").read_text()).get("pid")):
            raise Blocked("Previous coordinator is still alive")
        acknowledge_stop(root, (root / "stop.request").read_bytes())
    project_lock = Path(tempfile.gettempdir()) / "autodispatch-locks" / digest(str(plan.repo).lower().encode())
    with service_lease(root), CoordinatorLock(project_lock), CoordinatorLock(root):
        store = Store(root)
        service = {"pid": os.getpid(), "state": "RUNNING", "started_at": time.time(),
                   "workflow": "integrate-first-validate-last", "max_workers": worker_capacity(c, plan, approved['authorized_tasks']),
                   'worker_mode': c['max_workers'],
                   "max_agent_requests": c["max_agent_requests"], "batch_id": request["batch_id"]}
        atomic_json(root / "service.json", service)
        try:
            backend = BatchBackend(c, plan, store)
            backend.batch_id, backend.request = request["batch_id"], request
            validate_spec(plan.repo, c["manifest_sha256"])
            base = backend.current_base()
            before = store.records()
            archive = root / "reports" / f"batch-{backend.batch_id}"
            archive.mkdir(parents=True, exist_ok=True)
            if (archive / "before.json").exists():
                raise Blocked("Batch already attempted; inspect evidence, no automatic restart")
            atomic_json(archive / "before.json", before)
            atomic_json(archive / "request.json", request)
            atomic_json(root / "batch-active.json", {"batch_id": backend.batch_id, "request": str(request_path)})
            for tid, rec in before.items():
                if tid not in approved["authorized_tasks"] or tid in {"IP-00", "IP-34"}:
                    continue
                if process_alive(rec.get("pid")) or rec.get("pending_launch") or rec.get("local_merge_intent"):
                    raise Blocked(f"{tid} still has active/ambiguous ownership")
                if rec["state"] not in {"MERGED_VERIFIED", INTEGRATED}:
                    store.update(tid, "PENDING", resume_checkpoint=None, batch_id=backend.batch_id,
                                 reason="用户明确切换批量实现，全部集成后统一校验", pid=None)
            engine = BatchEngine(plan, store, backend, approved["authorized_tasks"], c["task_timeout_seconds"])
            result = engine.run(c["max_workers"], c["max_tasks"])
            records = store.records()
            missing = [tid for tid in approved["authorized_tasks"]
                       if records.get(tid, {}).get("state") not in {"MERGED_VERIFIED", INTEGRATED}]
            if missing or result.get("budget_exhausted") or (root / "stop.request").exists():
                service.update(state="BLOCKED", incomplete_tasks=missing,
                               budget_exhausted=result.get("budget_exhausted"), final_validation="NOT_RUN")
            else:
                service.update(phase="final-validation", heartbeat_at=time.time())
                atomic_json(root / "service.json", service)
                report, validation = final_checks(backend, plan, approved["authorized_tasks"])
                service.update(state=validation["status"], final_report=str(report))
                for tid, rec in records.items():
                    if rec.get("batch_id") == backend.batch_id and rec["state"] == INTEGRATED:
                        store.update(tid, batch_validation=validation["status"], batch_report=str(report),
                                     reason="批次完整性校验" + validation["status"] + "；见统一报告，独立审查与原生准入不自动通过")
        except Exception as exc:
            service.update(state="BLOCKED", reason=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            store.close()
            service.update(finished_at=time.time())
            atomic_json(root / "service.json", service)
    return service


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--batch-approval", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", required=True)
    args = parser.parse_args()
    print(json.dumps(run_batch(args.config, args.approval, args.batch_approval), ensure_ascii=False))


if __name__ == "__main__":
    main()

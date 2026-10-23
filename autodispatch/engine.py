from __future__ import annotations

import concurrent.futures
import json
import os
import tempfile
import time
from pathlib import Path

from .model import Blocked, Plan, PublicationPending, digest, validate_spec
from .safety import assert_review, classify_red, conflicts, require_approval
from .store import CoordinatorLock, Store, process_alive
from .verification import green, receipt_valid
from .workers import record_worker_issue, reserve_worker, worker_capacity

ACTIVE = {"RUNNING", "RED_READY", "RED_VERIFIED", "CODE_READY_VERIFY_PENDING", "REVIEWING"}
WAITING = {"WAITING_CI", "WAITING_PR", "WAITING_HUMAN_APPROVAL"}


class Engine:
    def __init__(self, plan, store, backend, authorization, timeout=3600):
        self.plan, self.store, self.backend = plan, store, backend
        self.authorization, self.timeout = authorization, timeout

    def ready(self, base):
        records = self.store.records()
        ready = []
        for task in self.plan.tasks.values():
            if task.id not in self.authorization or task.id in {"IP-00", "IP-34"}:
                continue
            current = records.get(task.id, {})
            if current.get("state", "PENDING") not in {"PENDING", "READY"}:
                continue
            if not all(self.backend.dependency_valid(records.get(dep, {}), base) for dep in task.dependencies):
                continue
            holders = [self.plan.tasks[tid] for tid, rec in records.items() if tid in self.plan.tasks
                       and tid != task.id and rec.get("worktree") and rec.get("state") != "MERGED_VERIFIED"]
            if any(conflicts(task, held) for held in holders + ready):
                continue
            ready.append(task)
        return ready

    def task(self, task, base):
        saved = self.store.records().get(task.id, {})
        checkpoint = saved.get("resume_checkpoint")
        self.store.update(task.id, "RUNNING", executor=task.agent, base_sha=saved.get("base_sha", base),
                          coordinator_pid=os.getpid(), deadline=saved.get("deadline") or time.time() + self.timeout,
                          automatic_retries=saved.get("automatic_retries", 0))
        try:
            if checkpoint:
                work = {k: saved[k] for k in ("worktree", "branch", "base_sha")}
                if saved.get("red_sha"):
                    work["red_sha"] = saved["red_sha"]
                if saved.get("wsl_worktree"):
                    work["wsl_worktree"] = saved["wsl_worktree"]
                self.backend.inspect_work(saved)
            else:
                work = self.backend.prepare(task, base)
            self.store.update(task.id, **work)
            red = saved.get("red") if checkpoint else None
            if task.agent == "OpenCode":
                red_sha = work.get("red_sha") or self.backend.run_agent(task, "red", work)
                work["red_sha"] = red_sha
                if red is None:
                    self.store.update(task.id, "RED_READY", red_sha=red_sha, commit_sha=red_sha,
                                      writer_released=True)
                    red = self.backend.verify(task, red_sha, "red")
                if red["commit_sha"] != red_sha or not classify_red(red["exit_code"], red["stats"]):
                    raise Blocked("RED must be an actual expected assertion failure at the exact test SHA")
                assert_review(red["codex_verification"], task, red_sha)
                self.store.update(task.id, "RED_VERIFIED", red=red)
            sha = (saved["commit_sha"] if checkpoint in {"CODE_READY_VERIFY_PENDING", "REVIEWING", "WAITING_PR"}
                   else self.backend.run_agent(task, "implement", work, red))
            self.store.update(task.id, "CODE_READY_VERIFY_PENDING", commit_sha=sha, writer_released=True)
            evidence = saved.get("green") if checkpoint in {"REVIEWING", "WAITING_PR"} else None
            evidence = evidence or self.backend.verify(task, sha, "green")
            if evidence["commit_sha"] != sha:
                raise Blocked("GREEN result belongs to another commit")
            green(evidence)
            assert_review(evidence["codex_verification"], task, sha)
            self.store.update(task.id, "REVIEWING", green=evidence,
                              verification_receipts=[evidence["receipt"], *evidence.get("regressions", [])])
            review = saved.get("review") if checkpoint == "WAITING_PR" else None
            review = review or self.backend.independent_review(task, sha, evidence)
            assert_review(review, task, sha)
            self.store.update(task.id, "WAITING_PR", review=review, resume_checkpoint=None)
            if getattr(self.backend, "local_integration", False):
                result = self.backend.integrate_local(task, work, sha, evidence, review)
                self.store.update(task.id, "MERGED_VERIFIED", integration_target="local-dev", local_only=True,
                                  merge_sha=result["merge_sha"], verified_local_dev_sha=result["merge_sha"],
                                  integration=result["integration"], local_merge_intent=False,
                                  verification_receipts=[evidence["receipt"], *evidence.get("regressions", []),
                                                         result["integration"]["receipt"], *result["integration"].get("regressions", [])])
                return
            if self.backend.publication_deferred:
                self.store.update(task.id, "WAITING_PR", reason="Publication deferred by user: no push and no PR creation",
                                  publish_intent=False, pid=None, lease_until=None)
                return
            pr = self.backend.publish(task, work, sha, evidence, review)
            if pr.get("headRefOid") != sha or pr.get("baseRefName") != "dev":
                raise Blocked("PR SHA/base mismatch")
            self.store.update(task.id, "WAITING_CI", pr=pr, publish_intent=False)
            required = self.backend.github.required()
            if self.backend.github.ci_passed(pr, required):
                reason = "Required CI passed; human review/merge required"
            elif not required:
                reason = "Required CI not configured; early-task human gate; never automatic merge"
            else:
                reason = "Required CI missing/pending/failed; merge forbidden"
            self.store.update(task.id, "WAITING_HUMAN_APPROVAL" if not required or self.backend.github.ci_passed(pr, required)
                              else "WAITING_CI", reason=reason, pid=None, lease_until=None)
        except PublicationPending as exc:
            self.store.update(task.id, "WAITING_PR", reason=str(exc), publication_failed=True,
                              publish_intent=False, pid=None, lease_until=None)
        except (Blocked, OSError, ValueError, KeyError) as exc:
            self.store.update(task.id, "BLOCKED", reason=str(exc), pid=None, lease_until=None)
        except Exception as exc:
            self.store.update(task.id, "FAILED", reason=f"{type(exc).__name__}: {exc}", pid=None, lease_until=None)

    def recover(self):
        # No process restart, push, commit or PR creation during recovery inspection.
        for tid, rec in self.store.records().items():
            if tid not in self.plan.tasks:
                continue
            publication_can_resume = (rec.get("state") == "WAITING_PR" and rec.get("review")
                                      and not rec.get("publication_failed") and
                                      (getattr(self.backend, "local_integration", False) or not self.backend.publication_deferred))
            if rec.get("state") in ACTIVE or rec.get("pending_launch") or rec.get("publish_intent") or publication_can_resume:
                alive = process_alive(rec.get("pid"))
                inspection = {}
                try:
                    if rec.get("worktree"):
                        inspection = self.backend.inspect_work(rec)
                except (Blocked, OSError) as exc:
                    inspection = {"error": str(exc)}
                safe = (not alive and not rec.get("pending_launch") and inspection.get("clean")
                        and not rec.get("local_merge_intent")
                        and not (self.store.root / "local-merge-pending.json").exists()
                        and rec.get("state") in {"RED_READY", "RED_VERIFIED", "CODE_READY_VERIFY_PENDING", "REVIEWING", "WAITING_PR"})
                for key in ("red", "green"):
                    evidence = rec.get(key)
                    if evidence:
                        try:
                            safe = safe and receipt_valid(evidence["receipt"]) and digest(Path(evidence["report"]).read_bytes()) == evidence["report_sha256"]
                            if key == "green":
                                green(evidence)
                            assert_review(evidence["codex_verification"], self.plan.tasks[tid], evidence["commit_sha"])
                        except (Blocked, OSError, KeyError):
                            safe = False
                if rec.get("state") == "RED_VERIFIED" and not rec.get("red"):
                    safe = False
                if rec.get("state") in {"REVIEWING", "WAITING_PR"} and not rec.get("green"):
                    safe = False
                if safe:
                    self.store.update(tid, "READY", resume_checkpoint=rec["state"],
                                      reason="Recovered clean exact-SHA checkpoint; no previous process remains",
                                      pid=None, pending_launch=False, recovery_inspection=inspection)
                else:
                    self.store.update(tid, "BLOCKED", reason="Interrupted ambiguous stage: inspect process tree and Git; never auto-retry",
                                      recovered_process_alive=alive, recovery_inspection=inspection)

    def _run_worker(self, task, base):
        try:
            return self.task(task, base)
        finally:
            self.store.update(task.id, worker_active=False)

    def run(self, max_workers=None, max_tasks=2):
        if type(max_tasks) is not int or max_tasks < 1:
            raise Blocked("Finite positive limits required")
        config = getattr(self.backend, 'c', {})
        if any(rec.get('pending_launch') or process_alive(rec.get('pid'))
               for tid, rec in self.store.records().items() if tid in self.plan.tasks):
            return {'launched_tasks': 0, 'tasks': self.store.records(), 'auto_merge': False,
                    'budget_exhausted': False, 'blocked_reason': 'An existing or ambiguous task process still owns a worker'}
        policy = config.get('max_workers', 'auto') if max_workers is None else max_workers
        capacity = worker_capacity(dict(config, max_workers=policy), self.plan, self.authorization)
        from .safety import approval_fingerprint
        with self.store.lock, self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('scheduler', json.dumps({
                'configuration_sha256': approval_fingerprint(config), 'capacity': capacity,
                'authorized_tasks': list(self.authorization), 'mode': policy})))
        base = self.backend.current_base()
        launched = 0
        budget_exhausted = False
        unavailable = set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, capacity)) as pool:
            active = {}
            while True:
                # Saving a corrected preference clears its issue. Re-admit that
                # slot on the next scheduling pass instead of disabling it for the batch.
                with self.store.lock:
                    unavailable = {(slot, executor) for slot, executor in unavailable
                        if self.store.db.execute('SELECT 1 FROM meta WHERE key=?',
                            (f'worker:issue:{slot}:{executor}',)).fetchone()}
                request_limit = getattr(getattr(self.backend, "runner", None), "max_requests", None)
                budget_exhausted = request_limit is not None and self.store.agent_requests_used() >= request_limit
                if (self.store.root / "stop.request").exists() or budget_exhausted:
                    candidates = []
                else:
                    candidates = self.ready(base)
                for task in candidates:
                    if len(active) >= capacity or launched >= max_tasks:
                        break
                    if (self.store.root / 'stop.request').exists() or (request_limit is not None
                            and self.store.agent_requests_used() >= request_limit):
                        break
                    # Mark ownership before dispatching a worker.
                    used = {slot for _, slot in active.values()}
                    for slot in range(1, capacity + 1):
                        if slot in used or (slot, task.agent) in unavailable:
                            continue
                        try:
                            reserve_worker(self.store, config, task, slot, base)
                        except Blocked as exc:
                            record_worker_issue(self.store, config, slot, task.agent, exc)
                            unavailable.add((slot, task.agent))
                            continue
                        active[pool.submit(self._run_worker, task, base)] = (task.id, slot)
                        launched += 1
                        break
                if not active:
                    break
                done, _ = concurrent.futures.wait(active, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    future.result()
                    del active[future]
                # Re-read current remote for each subsequent wave, never reuse stale origin/dev.
                if launched < max_tasks:
                    base = self.backend.current_base()
        return {"launched_tasks": launched, "tasks": self.store.records(), "auto_merge": False,
                "budget_exhausted": budget_exhausted, 'worker_configuration_blocked': sorted(unavailable)}


def reconcile(backend, plan, store, approved):
    if getattr(backend, "local_integration", False):
        return reconcile_local(backend, plan, store, approved)
    base = backend.current_base()
    if backend.bootstrap_valid(base):
        store.update("IP-34", "MERGED_VERIFIED", bootstrap=True,
                     merge_sha="d5ed821a8c55376e2ea0be2fda0d72eaa9dd2f8e", verified_dev_sha=base)
    else:
        store.update("IP-34", "BLOCKED", reason="Remote bootstrap evidence unavailable")
    if "IP-00" not in store.records():
        store.update("IP-00", "WAITING_PR", branch="task/ip-00",
                     reason="Existing local task must be merged remotely and freshly verified; no reimplementation")
    for tid, rec in store.records().items():
        if tid == "IP-34" or tid not in plan.tasks or tid not in approved["authorized_tasks"]:
            continue
        if rec["state"] not in WAITING | {"MERGED_VERIFIED"} or not rec.get("branch"):
            continue
        pr = backend.github.pr(rec["branch"])
        if not pr or pr.get("state") != "MERGED":
            if rec["state"] == "MERGED_VERIFIED":
                store.update(tid, "BLOCKED", reason="Remote merged PR evidence disappeared")
            continue
        sha = pr["headRefOid"]
        if rec.get("commit_sha") and rec["commit_sha"] != sha:
            store.update(tid, "BLOCKED", reason="PR head changed after verification")
            continue
        merge = (pr.get("mergeCommit") or {}).get("oid")
        if not merge or pr.get("baseRefName") != "dev" or not backend.git.ancestor(backend.repo, merge, base):
            store.update(tid, "BLOCKED", reason="Merge is not in current origin/dev")
            continue
        required = backend.github.required()
        # A human may explicitly accept missing early CI for named, already merged PRs only.
        exception = pr["number"] in approved.get("early_ci_exception_pr_numbers", [])
        if not backend.github.ci_passed(pr, required) and not (not required and exception):
            store.update(tid, "WAITING_HUMAN_APPROVAL", reason="Merged PR lacks required CI proof; explicit early-CI exception required")
            continue
        try:
            observed = dict(rec, observed_dev_sha=base)
            if rec.get("state") == "MERGED_VERIFIED" and backend.dependency_valid(observed, base):
                # The original tested/reviewed commit remains unchanged and is still merged.
                # Observing a newer dev is not a claim that old tests ran at the new SHA.
                store.update(tid, observed_dev_sha=base)
                continue
            task = plan.tasks[tid]
            store.update(tid, "REVIEWING", deadline=time.time() + backend.c["task_timeout_seconds"], commit_sha=sha)
            # IP-00 legacy review predates fixes: never silently accept it; use fresh independent evidence.
            evidence = backend.verify(task, sha, "green")
            review = backend.independent_review(task, sha, evidence)
            assert_review(review, task, sha)
            integration = backend.verify(task, base, "green")
            green(integration)
            store.update(tid, "MERGED_VERIFIED", commit_sha=sha, merge_sha=merge, verified_dev_sha=base,
                         review=review, green=evidence, integration=integration, pr=pr,
                         verification_receipts=[evidence["receipt"], integration["receipt"],
                                                *evidence.get("regressions", []), *integration.get("regressions", [])])
        except (Blocked, OSError, ValueError, KeyError) as exc:
            store.update(tid, "BLOCKED", reason=str(exc))


def reconcile_local(backend, plan, store, approved):
    """Adopt existing local commits; never claim remote merge or CI success."""
    base = backend.current_base()
    if not backend.bootstrap_valid(base):
        raise Blocked("Existing IP-34 local commit/evidence did not verify")
    store.update("IP-34", "MERGED_VERIFIED", bootstrap=True, integration_target="local-dev", local_only=True,
                 merge_sha="d5ed821a8c55376e2ea0be2fda0d72eaa9dd2f8e", verified_local_dev_sha=base)
    rec = store.records().get("IP-00", {})
    if "IP-00" not in approved["authorized_tasks"]:
        return
    if backend.dependency_valid(rec, base):
        return
    # Verify the already integrated task, rather than reimplementing IP-00.
    original = backend.git.sha(backend.repo, "task/ip-00")
    if not backend.git.ancestor(backend.repo, original, base):
        store.update("IP-00", "WAITING_HUMAN_APPROVAL", reason="Existing task/ip-00 is not in local dev")
        return
    if rec.get("state") in {"BLOCKED", "FAILED"}:
        return
    task = plan.tasks["IP-00"]
    store.update(task.id, "REVIEWING", branch="task/ip-00", commit_sha=base,
                 deadline=time.time() + backend.c["task_timeout_seconds"], integration_target="local-dev")
    try:
        evidence = backend.verify(task, base, "green")
        review = backend.independent_review(task, base, evidence)
        assert_review(review, task, base)
        store.update(task.id, "MERGED_VERIFIED", integration_target="local-dev", local_only=True,
                     merge_sha=base, verified_local_dev_sha=base, commit_sha=base, green=evidence, review=review,
                     verification_receipts=[evidence["receipt"], *evidence.get("regressions", [])])
    except (Blocked, OSError, ValueError, KeyError) as exc:
        store.update(task.id, "BLOCKED", reason=str(exc))


def execute(c, approval_path, resume, max_workers=None, max_tasks=None):
    marker = Path(c["state_dir"]) / "stop.request"
    observed_stop = marker.read_bytes() if resume and marker.exists() else None
    approved = require_approval(c, approval_path)
    plan = Plan(Path(c["repo"]), c["pins"])
    approved_workers = worker_capacity(c, plan, approved['authorized_tasks'])
    for name, override, limit in (("max_workers", max_workers, approved_workers), ("max_tasks", max_tasks, c['max_tasks'])):
        if override is not None and (type(override) is not int or not 1 <= override <= limit):
            raise Blocked(f"{name} override may only reduce the approved limit")
    validate_spec(plan.repo, c["manifest_sha256"])
    from .doctor import diagnose
    diagnosis = diagnose(c, online=c.get("integration_target") != "local-dev")
    blockers = [x for x in diagnosis["checks"] if x["status"] == "BLOCKED"]
    if blockers:
        raise Blocked("Preflight failed: " + "; ".join(x["check"] + ": " + str(x["detail"]) for x in blockers))
    from .adapters import RealBackend
    # Stable project lock prevents different state directories from concurrently dispatching this repo.
    project_lock = Path(tempfile.gettempdir()) / "aethertrade-autodispatch-locks" / digest(str(plan.repo).casefold().encode())
    with CoordinatorLock(project_lock), CoordinatorLock(Path(c["state_dir"])):
        if resume:
            from .control import acknowledge_stop
            acknowledge_stop(Path(c["state_dir"]), observed_stop)
        store = Store(Path(c["state_dir"]))
        try:
            backend = RealBackend(c, plan, store)
            engine = Engine(plan, store, backend, approved["authorized_tasks"], c["task_timeout_seconds"])
            # Both run and resume recover first, so run cannot bypass crash checks.
            engine.recover()
            reconcile(backend, plan, store, approved)
            return engine.run(max_workers or c["max_workers"], max_tasks or c["max_tasks"])
        finally:
            store.close()

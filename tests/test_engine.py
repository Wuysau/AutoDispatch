import dataclasses
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from autodispatch.engine import Engine
from autodispatch.fake import FakeBackend, seed_dependencies
from autodispatch.model import Blocked, Plan, load_config
from autodispatch.process import Runner
from autodispatch.safety import assert_review, classify_red, conflicts, require_approval, approval_fingerprint, redact
from autodispatch.store import CoordinatorLock, Store, process_alive

ROOT = Path(__file__).resolve().parents[1]


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = load_config(ROOT / "config.example.json")
        cls.plan = Plan(Path(cls.c["repo"]), cls.c["pins"])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.backend = FakeBackend(self.root, self.plan, self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def engine(self, tasks=("IP-05",), seed=True):
        if seed:
            seed_dependencies(self.plan, self.store, tasks, self.backend.current_base())
        return Engine(self.plan, self.store, self.backend, tasks)

    def test_unmet_dependencies_launch_nothing(self):
        result = self.engine(seed=False).run()
        self.assertEqual(0, result["launched_tasks"])
        self.assertEqual([], self.backend.calls)

    def test_ip00_unmerged_blocks_ip35(self):
        self.store.update("IP-00", "WAITING_PR")
        self.assertEqual(0, self.engine(("IP-35",), seed=False).run()["launched_tasks"])

    def test_opencode_four_stages_and_manual_merge(self):
        e = self.engine()
        e.run()
        rec = self.store.records()["IP-05"]
        self.assertEqual("WAITING_HUMAN_APPROVAL", rec["state"])
        stages = [stage for tid, stage in self.backend.calls if tid == "IP-05"]
        self.assertEqual(["red", "test", "red-check", "implement", "test", "green-check", "review"], stages)
        self.assertEqual(1, rec["red"]["exit_code"])
        self.assertEqual(0, rec["green"]["exit_code"])
        self.assertNotEqual(rec["red_sha"], rec["commit_sha"])
        self.assertNotEqual(rec["red"]["receipt"]["cwd"], rec["green"]["receipt"]["cwd"])
        self.assertEqual(0, self.backend.github.merges)
        self.backend.human_merge("IP-05")
        self.assertEqual("MERGED_VERIFIED", self.store.records()["IP-05"]["state"])

    def test_all_five_opencode_routes_execute_red(self):
        for tid in ("IP-05", "IP-20", "IP-28", "IP-29", "IP-31"):
            with self.subTest(tid=tid):
                self.engine((tid,)).run(max_tasks=1)
                self.assertIn((tid, "red"), self.backend.calls)
                self.assertEqual("WAITING_HUMAN_APPROVAL", self.store.records()[tid]["state"])

    def test_codex_implementation_machine_review_pr(self):
        self.engine(("IP-35",)).run()
        self.assertEqual("WAITING_HUMAN_APPROVAL", self.store.records()["IP-35"]["state"])
        self.assertNotIn(("IP-35", "red"), self.backend.calls)

    def test_deferred_publication_never_pushes_or_creates_pr(self):
        self.backend.publication_deferred = True
        self.engine(("IP-35",)).run()
        self.assertEqual("WAITING_PR", self.store.records()["IP-35"]["state"])
        self.assertEqual(0, self.backend.github.created)
        self.assertEqual({}, self.backend.github.prs)

    def test_new_publication_authorization_resumes_without_recoding(self):
        self.backend.publication_deferred = True
        engine = self.engine(("IP-35",))
        engine.run()
        calls = len(self.backend.calls)
        self.backend.publication_deferred = False
        engine.recover()
        engine.run()
        self.assertEqual(calls, len(self.backend.calls))
        self.assertEqual(1, self.backend.github.created)

    def test_quota_error_blocks_no_retry(self):
        self.backend.faults[("IP-05", "red")] = "quota"
        e = self.engine()
        e.run()
        e.run()
        self.assertEqual("BLOCKED", self.store.records()["IP-05"]["state"])
        self.assertEqual(1, len(self.backend.calls))

    def test_persistent_request_budget_survives_restart(self):
        self.backend.runner.max_requests = 1
        self.engine().run()
        self.assertEqual("BLOCKED", self.store.records()["IP-05"]["state"])
        self.store.close()
        self.store = Store(self.root)
        with self.assertRaises(Blocked):
            self.store.charge(1)

    def test_environment_failure_is_not_red(self):
        self.backend.faults[("IP-05", "test")] = "environment"
        self.engine().run()
        self.assertEqual("BLOCKED", self.store.records()["IP-05"]["state"])
        self.assertNotIn(("IP-05", "implement"), self.backend.calls)

    def test_review_rejected_never_publishes(self):
        self.backend.faults[("IP-05", "review")] = "review-failed"
        self.engine().run()
        self.assertEqual("BLOCKED", self.store.records()["IP-05"]["state"])
        self.assertEqual(0, self.backend.github.created)

    def test_missing_ci_never_merges(self):
        self.backend.github.checks = []
        self.engine().run()
        with self.assertRaises(Blocked):
            self.backend.human_merge("IP-05")

    def test_failed_ci_never_merges(self):
        self.backend.github.ci = "FAILURE"
        self.engine().run()
        self.assertEqual("WAITING_CI", self.store.records()["IP-05"]["state"])
        with self.assertRaises(Blocked):
            self.backend.human_merge("IP-05")

    def test_stale_commit_and_missing_log_cannot_complete(self):
        self.engine().run()
        rec = self.store.records()["IP-05"]
        Path(rec["green"]["receipt"]["log"]).write_text("tampered", encoding="utf-8")
        with self.assertRaises(Blocked):
            self.backend.human_merge("IP-05")
        report = dict(rec["review"], commit_sha="0" * 40)
        with self.assertRaises(Blocked):
            assert_review(report, self.plan.tasks["IP-05"], rec["commit_sha"])

    def test_restart_does_not_repeat_pr_or_agent(self):
        e = self.engine()
        e.run()
        count = len(self.backend.calls)
        e.recover()
        e.run()
        self.assertEqual(count, len(self.backend.calls))
        self.assertEqual(1, self.backend.github.created)

    def test_resume_clean_verified_checkpoint_does_not_repeat_implementation(self):
        engine = self.engine()
        engine.run()
        before = sum(stage in {"red", "implement"} for _, stage in self.backend.calls)
        self.store.update("IP-05", "CODE_READY_VERIFY_PENDING", pid=None, pending_launch=False)
        engine.recover()
        self.assertEqual("READY", self.store.records()["IP-05"]["state"])
        engine.run()
        self.assertEqual(before, sum(stage in {"red", "implement"} for _, stage in self.backend.calls))
        self.assertEqual(1, self.backend.github.created)
        self.assertEqual("WAITING_HUMAN_APPROVAL", self.store.records()["IP-05"]["state"])

    def test_resume_dirty_checkpoint_stays_blocked(self):
        engine = self.engine()
        engine.run()
        rec = self.store.records()["IP-05"]
        (Path(rec["worktree"]) / "implementation.py").write_text("dirty user edit", encoding="utf-8")
        self.store.update("IP-05", "CODE_READY_VERIFY_PENDING", pid=None, pending_launch=False)
        engine.recover()
        self.assertEqual("BLOCKED", self.store.records()["IP-05"]["state"])

    def test_orphan_process_is_inspected_never_restarted(self):
        self.store.update("IP-05", "RUNNING", pid=os.getpid(), pending_launch=True)
        e = self.engine()
        e.recover()
        e.run()
        rec = self.store.records()["IP-05"]
        self.assertEqual("BLOCKED", rec["state"])
        self.assertTrue(rec["recovered_process_alive"])
        self.assertEqual([], self.backend.calls)

    def test_scope_conflict_including_case_and_directory(self):
        a = dataclasses.replace(self.plan.tasks["IP-05"], scope_write=("src/shared/**",))
        b = dataclasses.replace(self.plan.tasks["IP-20"], scope_write=("SRC/shared/a.py",))
        self.assertTrue(conflicts(a, b))

    def test_worktree_collision_blocks(self):
        (self.root / "worktrees" / "IP-05").mkdir(parents=True)
        self.engine().run()
        self.assertEqual("BLOCKED", self.store.records()["IP-05"]["state"])
        self.assertEqual([], self.backend.calls)

    def test_unrelated_task_continues_after_failure(self):
        self.backend.faults[("IP-05", "red")] = "quota"
        engine = self.engine(("IP-05", "IP-35"))
        self.store.update("IP-35", "PENDING")
        engine.run(max_workers=2, max_tasks=2)
        self.assertEqual("BLOCKED", self.store.records()["IP-05"]["state"])
        self.assertEqual("WAITING_HUMAN_APPROVAL", self.store.records()["IP-35"]["state"])

    def test_os_lock_blocks_second_instance(self):
        with CoordinatorLock(self.root):
            with self.assertRaises(Blocked):
                with CoordinatorLock(self.root):
                    pass
        with CoordinatorLock(self.root):
            pass

    def test_subprocess_timeout_and_log_redaction(self):
        runner = Runner(self.root)
        out = runner.run([sys.executable, "-c", "print('api_key=sample-secret', flush=True); import time; time.sleep(5)"], self.root, 0.2)
        self.assertEqual(124, out.code)
        self.assertNotIn("sample-secret", Path(out.receipt["log"]).read_text())

    def test_child_environment_excludes_credentials(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"BINANCE_API_KEY": "do-not-inherit"}):
            result = Runner().run([sys.executable, "-c", "import os; print(os.getenv('BINANCE_API_KEY', 'ABSENT'))"], self.root, 5)
        self.assertEqual("ABSENT", result.output.strip())

    def test_approval_denied_and_fingerprint_bound(self):
        path = self.root / "approval.json"
        path.write_text(json.dumps({"configuration_sha256": approval_fingerprint(self.c)}), encoding="utf-8")
        with self.assertRaises(Blocked):
            require_approval(self.c, path)


if __name__ == "__main__":
    unittest.main()

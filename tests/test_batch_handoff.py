import json
import pathlib
import tempfile
import threading
import time
import unittest

from autodispatch.control import request_stop, status
from autodispatch.handoff import service_lease
from autodispatch.model import Blocked, Plan, load_config
from autodispatch.store import Store, CoordinatorLock
from autodispatch.fake import FakeBackend, seed_dependencies
from autodispatch.engine import Engine

ROOT = pathlib.Path(__file__).resolve().parents[1]


class BatchHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.c = load_config(ROOT / "config.example.json")
        self.c["state_dir"] = str(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_successor_waits_for_old_lease_without_interrupting_it(self):
        entered = threading.Event()
        errors = []
        def successor():
            try:
                with service_lease(self.root, wait=True, timeout=3):
                    entered.set()
            except Exception as exc:
                errors.append(exc)
        with CoordinatorLock(self.root / "service-lock"):
            worker = threading.Thread(target=successor)
            worker.start()
            time.sleep(0.25)
            self.assertFalse(entered.is_set())
            saved = json.loads((self.root / "handoff.json").read_text())
            self.assertEqual("WAITING_FOR_IDLE", saved["state"])
        worker.join(timeout=4)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)
        self.assertTrue(entered.is_set())
        self.assertEqual("STARTED", json.loads((self.root / "handoff.json").read_text())["state"])

    def test_stop_cancels_pending_handoff_without_starting_service(self):
        errors = []
        entered = threading.Event()
        def successor():
            try:
                with service_lease(self.root, wait=True, timeout=3):
                    entered.set()
            except Blocked as exc:
                errors.append(str(exc))
        with CoordinatorLock(self.root / "service-lock"):
            worker = threading.Thread(target=successor)
            worker.start()
            time.sleep(0.25)
            request_stop(self.c)
            worker.join(timeout=3)
        self.assertFalse(entered.is_set())
        self.assertTrue(errors)
        self.assertEqual("STOPPED", json.loads((self.root / "handoff.json").read_text())["state"])

    def test_handoff_timeout_is_bounded(self):
        with CoordinatorLock(self.root / "service-lock"):
            with self.assertRaisesRegex(Blocked, "expired"):
                with service_lease(self.root, wait=True, timeout=1):
                    self.fail("must not acquire busy service")
        self.assertEqual("TIMED_OUT", json.loads((self.root / "handoff.json").read_text())["state"])

    def test_exhausted_budget_does_not_create_new_worktrees_or_agents(self):
        store = Store(self.root)
        try:
            plan = Plan(pathlib.Path(self.c["repo"]), self.c["pins"])
            backend = FakeBackend(self.root, plan, store, max_requests=1)
            seed_dependencies(plan, store, ["IP-35"], backend.current_base())
            store.charge(1)
            result = Engine(plan, store, backend, ["IP-35"]).run()
            self.assertEqual(0, result["launched_tasks"])
            self.assertTrue(result["budget_exhausted"])
            self.assertEqual([], backend.calls)
            self.assertFalse((self.root / "worktrees").exists())
        finally:
            store.close()

    def test_increasing_limit_preserves_already_charged_calls(self):
        store = Store(self.root)
        try:
            store.charge(12)
            store.charge(12)
        finally:
            store.close()
        self.c["max_agent_requests"] = 240
        self.assertEqual(2, status(self.c)["budget"]["agent_cli_used"])
        store = Store(self.root)
        try:
            self.assertEqual(3, store.charge(240))
        finally:
            store.close()

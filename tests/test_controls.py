import copy
import json
import sys
import tempfile
import threading
import time
import unittest
import subprocess
import os
from pathlib import Path
from unittest.mock import patch

from autodispatch.control import request_stop, status, acknowledge_stop
from autodispatch.engine import Engine
from autodispatch.fake import FakeBackend, seed_dependencies
from autodispatch.model import Blocked, Plan, PublicationPending, load_config
from autodispatch.process import Runner
from autodispatch.service import admission, serve
from autodispatch.store import Store

ROOT = Path(__file__).resolve().parents[1]


class ControlsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.c = load_config(ROOT / "config.example.json")
        self.c["state_dir"] = str(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_stop_is_persistent_and_prevents_new_processes(self):
        self.assertEqual("STOP_REQUESTED", request_stop(self.c)["status"])
        with self.assertRaises(Blocked):
            Runner(self.root).run([sys.executable, "-c", "raise Exception('must not run')"], self.root, 3)
        self.assertTrue(status(self.c)["stop_requested"])

    def test_resume_acknowledges_only_the_stop_version_it_observed(self):
        request_stop(self.c)
        old = (self.root / "stop.request").read_bytes()
        request_stop(self.c)
        self.assertFalse(acknowledge_stop(self.root, old))
        self.assertTrue((self.root / "stop.request").exists())
        current = (self.root / "stop.request").read_bytes()
        self.assertTrue(acknowledge_stop(self.root, current))
        self.assertFalse((self.root / "stop.request").exists())

    def test_service_cycle_does_not_acknowledge_a_new_stop(self):
        def admit(*_):
            request_stop(self.c)
            return {"blockers": []}, {}
        with patch("autodispatch.service.admission", side_effect=admit), patch("autodispatch.engine.execute", return_value={"launched_tasks": 0}) as execute:
            serve(self.c, ROOT / "config.example.json", self.root / "approval.json", 1, 1)
        self.assertFalse(execute.call_args.args[2])
        self.assertTrue((self.root / "stop.request").exists())

    def test_clean_local_review_checkpoint_recovers_with_publication_deferred(self):
        store = Store(self.root)
        try:
            plan = Plan(Path(self.c["repo"]), self.c["pins"])
            backend = FakeBackend(self.root, plan, store)
            backend.publication_deferred = True
            seed_dependencies(plan, store, ["IP-35"], backend.current_base())
            engine = Engine(plan, store, backend, ["IP-35"])
            engine.run()
            self.assertEqual("WAITING_PR", store.records()["IP-35"]["state"])
            backend.local_integration = True
            engine.recover()
            self.assertEqual("READY", store.records()["IP-35"]["state"])
            self.assertEqual("WAITING_PR", store.records()["IP-35"]["resume_checkpoint"])
        finally:
            store.close()

    def test_local_task_flow_runs_tests_review_and_merges_without_publication(self):
        store = Store(self.root)
        try:
            plan = Plan(Path(self.c["repo"]), self.c["pins"])
            backend = FakeBackend(self.root, plan, store)
            backend.publication_deferred = True
            backend.local_integration = True
            def integrate(task, work, sha, evidence, review):
                integration = backend.verify(task, sha, "green")
                return {"merge_sha": backend.current_base(), "integration": integration}
            backend.integrate_local = integrate
            seed_dependencies(plan, store, ["IP-35"], backend.current_base())
            with patch.object(backend, "publish", side_effect=AssertionError("No remote publication allowed")):
                Engine(plan, store, backend, ["IP-35"]).run()
            record = store.records()["IP-35"]
            self.assertEqual("MERGED_VERIFIED", record["state"])
            self.assertEqual("local-dev", record["integration_target"])
            self.assertTrue(record["local_only"])
            self.assertEqual(0, record["integration"]["exit_code"])
            self.assertEqual(0, backend.github.created)
        finally:
            store.close()

    def test_stop_interrupts_owned_process_and_retains_log(self):
        threading.Timer(0.3, lambda: request_stop(self.c)).start()
        result = Runner(self.root).run([sys.executable, "-c", "import time; time.sleep(10)"], self.root, 5)
        self.assertEqual(130, result.code)
        self.assertTrue(result.receipt["stopped"])
        self.assertTrue(Path(result.receipt["log"]).is_file())

    @unittest.skipUnless(os.name == "nt", "Windows detached console encoding regression")
    def test_detached_runner_keeps_nested_powershell_stderr_utf8(self):
        nested = ("import subprocess; r=subprocess.run(['pwsh','-NoProfile','-Command',"
                  + repr("throw 'Target is not empty 中文'")
                  + "],capture_output=True,text=True,encoding='utf-8'); assert r.returncode != 0; assert 'Target is not empty' in r.stderr; assert '中文' in r.stderr")
        driver = ("from pathlib import Path; from autodispatch.process import Runner; import sys; "
                  "r=Runner().run([sys.executable,'-c'," + repr(nested) + "],Path.cwd(),10); "
                  "print(r.output); raise SystemExit(r.code)")
        log = self.root / "detached-encoding.log"
        with log.open("wb") as output:
            child = subprocess.Popen([sys.executable, "-c", driver], cwd=ROOT, stdin=subprocess.DEVNULL,
                                     stdout=output, stderr=subprocess.STDOUT,
                                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
            child.wait(timeout=20)
        self.assertEqual(0, child.returncode, log.read_text(encoding="utf-8", errors="replace"))

    def test_failed_admission_persists_all_39_without_models(self):
        diagnosis = {"checks": [{"check": "remote", "status": "BLOCKED", "detail": "network unavailable"}], "agent_calls": 0}
        report, approved = admission(self.c, self.root / "absent-approval.json", diagnosis)
        self.assertEqual("BLOCKED", report["status"])
        self.assertIsNone(approved)
        records = Store.snapshot(self.root)
        self.assertEqual(39, len(records))
        self.assertEqual("WAITING_HUMAN_APPROVAL", records["IP-00"]["state"])
        self.assertTrue((self.root / "admission.json").is_file())

    def test_service_exits_on_gate_failure_and_records_pid(self):
        diagnosis = {"checks": [{"check": "remote", "status": "BLOCKED", "detail": "network unavailable"}], "agent_calls": 0}
        with patch("autodispatch.service.diagnose", return_value=diagnosis):
            result = serve(self.c, ROOT / "config.example.json", self.root / "missing.json", 2, 1)
        self.assertEqual("BLOCKED", result["status"])
        saved = status(self.c)["service"]
        self.assertEqual("BLOCKED", saved["state"])
        self.assertIn("finished_at", saved)

    def test_push_failure_retains_local_success_and_does_not_repeat(self):
        store = Store(self.root)
        try:
            plan = Plan(Path(self.c["repo"]), self.c["pins"])
            backend = FakeBackend(self.root, plan, store)
            seed_dependencies(plan, store, ["IP-05", "IP-35"], backend.current_base())
            engine = Engine(plan, store, backend, ["IP-05", "IP-35"])
            original = backend.publish
            attempts = []
            def publish(task, *args):
                if task.id == "IP-05":
                    attempts.append(task.id)
                    raise PublicationPending("fake push failed once")
                return original(task, *args)
            with patch.object(backend, "publish", side_effect=publish):
                engine.run(max_workers=2)
                engine.recover()
                engine.run(max_workers=2)
            self.assertEqual(["IP-05"], attempts)
            rec = store.records()
            self.assertEqual("WAITING_PR", rec["IP-05"]["state"])
            self.assertEqual(0, rec["IP-05"]["green"]["exit_code"])
            self.assertEqual("WAITING_HUMAN_APPROVAL", rec["IP-35"]["state"])
        finally:
            store.close()

    def test_agent_usage_and_model_are_saved_when_available(self):
        store = Store(self.root)
        try:
            output = json.dumps({"type": "turn.completed", "model": "fake-model", "usage": {"input_tokens": 20, "output_tokens": 3}})
            result = Runner(self.root, store, 1).run([sys.executable, "-c", "print(" + repr(output) + ")"],
                                                    self.root, 5, tid="IP-05", stage="usage-test", agent=True)
            self.assertEqual({"input_tokens": 20, "output_tokens": 3}, result.receipt["usage"])
            self.assertEqual(["fake-model"], result.receipt["reported_models"])
            self.assertGreaterEqual(result.receipt["elapsed_seconds"], 0)
        finally:
            store.close()

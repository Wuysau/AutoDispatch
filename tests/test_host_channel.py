import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from autodispatch.api import make_server


class HostChannelTests(unittest.TestCase):
    def test_window_commands_can_be_isolated_from_live_task_state(self):
        from autodispatch.api import DispatchApi
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = root / 'tasks'
            tasks.mkdir()
            api = DispatchApi({'state_dir': str(tasks), 'ui_state_dir': str(root / 'ui'), 'repo': str(root), 'pins': {}})
            api.set_ui_request({'action': 'hide', 'target': 'widget'})
            self.assertTrue((root / 'ui' / 'ui-host.json').exists())
            self.assertEqual(list(tasks.iterdir()), [])
            self.assertEqual(api.root, tasks)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        cfg = {"state_dir": str(root / "state"), "repo": str(root), "pins": {}}
        self.server = make_server(cfg, port=0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def post(self, path, payload):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_size_request_is_stored_for_the_host(self):
        status, body = self.post("/api/autodispatch/ui/host",
                                 {"target": "widget", "action": "size", "width": 444, "height": 283})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        status, state = self.get("/api/autodispatch/ui/host")
        self.assertEqual(status, 200)
        request = state["requests"]["widget"]
        self.assertEqual(request["action"], "size")
        self.assertEqual(request["width"], 444)
        self.assertEqual(request["height"], 283)
        self.assertEqual(request["seq"], body["seq"])

    def test_each_target_keeps_its_own_request(self):
        self.post("/api/autodispatch/ui/host", {"target": "widget", "action": "size", "width": 52, "height": 52})
        self.post("/api/autodispatch/ui/host", {"target": "console", "action": "size", "width": 960, "height": 800})
        _, state = self.get("/api/autodispatch/ui/host")
        self.assertEqual(state["requests"]["widget"]["width"], 52)
        self.assertEqual(state["requests"]["console"]["width"], 960)

    def test_same_target_commands_are_queued_and_metadata_cannot_be_spoofed(self):
        self.post("/api/autodispatch/ui/host", {"action": "size", "width": 440, "height": 310, "seq": -1, "at": 0})
        self.post("/api/autodispatch/ui/host", {"action": "hide"})
        data = json.loads((Path(self.server.api.root) / "ui-host.json").read_text())
        self.assertEqual([r["action"] for r in data["commands"]], ["size", "hide"])
        self.assertGreater(data["commands"][0]["at"], 0)
        self.assertGreater(data["commands"][1]["seq"], data["commands"][0]["seq"])

    def test_receipt_identifies_own_command_even_when_next_request_interleaves(self):
        api = self.server.api
        real_lock = api._ui_lock
        class InterleavedLock:
            def __enter__(self):
                real_lock.acquire()
            def __exit__(self, *exc):
                real_lock.release()
                api._ui_lock = real_lock
                api.set_ui_request({"action": "hide", "target": "console"})
        api._ui_lock = InterleavedLock()
        result = api.set_ui_request({"action": "show", "target": "widget"})
        self.assertEqual(result["seq"], api._ui_requests["widget"]["seq"])
        self.assertNotEqual(result["seq"], api._ui_requests["console"]["seq"])

    def test_window_arguments_are_validated(self):
        for body in [{"action": "size", "width": -1, "height": 200},
                     {"action": "pin", "value": "false"},
                     {"action": "console", "task_id": "https://example.com"}]:
            self.assertEqual(self.post("/api/autodispatch/ui/host", body)[0], 400)
        status, _ = self.post("/api/autodispatch/ui/host", {"action": "console", "task_id": "IP-35"})
        self.assertEqual(status, 200)

    def test_unknown_action_and_target_are_refused(self):
        status, body = self.post("/api/autodispatch/ui/host", {"action": "explode"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        status, _ = self.post("/api/autodispatch/ui/host", {"action": "size", "target": "other"})
        self.assertEqual(status, 400)

    def test_heartbeat_marks_the_host_as_present(self):
        _, state = self.get("/api/autodispatch/ui/host")
        self.assertFalse(state["host"])
        root = Path(self.server.api.root)
        root.mkdir(parents=True, exist_ok=True)
        (root / "ui-host-alive").write_text("1", encoding="utf-8")
        _, state = self.get("/api/autodispatch/ui/host")
        self.assertTrue(state["host"])

    def test_request_is_published_to_the_state_directory(self):
        self.post("/api/autodispatch/ui/host",
                  {"target": "widget", "action": "size", "width": 444, "height": 283})
        path = Path(self.server.api.root) / "ui-host.json"
        self.assertTrue(path.is_file())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["requests"]["widget"]["width"], 444)
        self.assertEqual(data["requests"]["widget"]["height"], 283)


if __name__ == "__main__":
    unittest.main()

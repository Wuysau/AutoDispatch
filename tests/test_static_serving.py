import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from autodispatch.api import make_server


class StaticServingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.dist = root / "dist"
        (self.dist / "assets").mkdir(parents=True)
        (self.dist / "index.html").write_text("<html>app</html>", encoding="utf-8")
        (self.dist / "assets" / "index-abc.js").write_text("console.log(1)", encoding="utf-8")
        (root / "secret.txt").write_text("nope", encoding="utf-8")
        cfg = {"state_dir": str(root / "state"), "repo": str(root), "pins": {}}
        self.server = make_server(cfg, port=0, static_dir=self.dist)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as res:
                return res.status, res.headers, res.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def test_index_and_hash_routes_serve_app(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"app", body)
        self.assertEqual(headers["Cache-Control"], "no-cache")
        status, _, body = self.get("/some/route")
        self.assertEqual(status, 200)
        self.assertIn(b"app", body)

    def test_hashed_assets_are_cached_long(self):
        status, headers, body = self.get("/assets/index-abc.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["Content-Type"])
        self.assertIn("immutable", headers["Cache-Control"])
        self.assertEqual(body, b"console.log(1)")

    def test_path_traversal_is_refused(self):
        status, _, body = self.get("/../secret.txt")
        self.assertEqual(status, 404)
        self.assertNotIn(b"nope", body)

    def test_snapshot_returns_304_when_unchanged(self):
        self.server.api.snapshot = lambda: {"n": 1}
        status, headers, _ = self.get("/api/autodispatch/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-cache")
        req = urllib.request.Request(self.base + "/api/autodispatch/snapshot", headers={"If-None-Match": headers["ETag"]})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 304)
        ctx.exception.close()

    def test_unknown_api_path_returns_json_not_html(self):
        status, headers, body = self.get("/api/autodispatch/unknown")
        self.assertEqual(status, 404)
        self.assertIn("json", headers["Content-Type"])
        self.assertEqual(json.loads(body), {"error": "not found"})


if __name__ == "__main__":
    unittest.main()

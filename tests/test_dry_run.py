import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from autodispatch.cli import main
from autodispatch.doctor import planning
from autodispatch.model import Blocked, Plan, load_config, relative, validate_spec
from autodispatch.safety import verification_argv, wsl_argv

ROOT = Path(__file__).resolve().parents[1]


class DryRunTests(unittest.TestCase):
    def setUp(self):
        self.c = load_config(ROOT / "config.example.json")

    def test_real_graph_is_39_with_exact_five_opencode(self):
        plan = Plan(Path(self.c["repo"]), self.c["pins"])
        self.assertEqual(39, len(plan.tasks))
        self.assertEqual({"IP-05", "IP-20", "IP-28", "IP-29", "IP-31"},
                         {t.id for t in plan.tasks.values() if t.agent == "OpenCode"})

    def test_plan_has_no_state_side_effect_and_blocks_ip00(self):
        with tempfile.TemporaryDirectory() as d:
            self.c["state_dir"] = str(Path(d) / "absent")
            result = planning(self.c)
            self.assertFalse(Path(self.c["state_dir"]).exists())
            states = {x["task_id"]: x for x in result["tasks"]}
            self.assertEqual("WAITING_PR", states["IP-00"]["state"])
            self.assertIn("IP-00", states["IP-35"]["reason"])
            self.assertEqual(0, result["agent_calls"])

    def test_run_without_execute_is_read_only(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(0, main(["run"]))
        self.assertEqual("READ_ONLY", json.loads(out.getvalue())["mode"])

    def test_original_23_bytes(self):
        self.assertEqual(23, len(validate_spec(Path(self.c["repo"]), self.c["manifest_sha256"])))
        with self.assertRaises(Blocked):
            validate_spec(Path(self.c["repo"]), "0" * 64)

    def test_command_injection_and_traversal_blocked(self):
        for path in ("../secret", "C:/secrets", "x/../../y", "x;echo", "a\\b", "/etc/passwd"):
            with self.subTest(path=path), self.assertRaises(Blocked):
                relative(path)
        for cmd in ("python -m pytest tests/x.py -v && evil", "python -m pytest ../x.py -v", "npm test; evil"):
            with self.subTest(cmd=cmd), self.assertRaises(Blocked):
                verification_argv(cmd, "python", ["npm"])

    def test_wsl_argv_does_not_use_shell_or_map_windows(self):
        hostile = "value ' ; $(touch /tmp/pwn)"
        args = wsl_argv("Ubuntu", "/home/user/work tree", ["opencode", "run", "--", hostile])
        self.assertEqual(hostile, args[-1])
        self.assertNotIn("bash", args)
        with self.assertRaises(Blocked):
            wsl_argv("Ubuntu", "D:/project", ["opencode"])


if __name__ == "__main__":
    unittest.main()

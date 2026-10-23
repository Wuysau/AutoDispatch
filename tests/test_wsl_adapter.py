import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from autodispatch.safety import wsl_argv
from autodispatch.model import Blocked
from autodispatch.wsl_adapter import WslAdapter
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


class WslAdapterTests(unittest.TestCase):
    def test_local_dev_fences_recovered_wsl_stage_before_any_remote_call(self):
        backend = SimpleNamespace(c={"integration_target": "local-dev", "wsl": {}})
        adapter = WslAdapter(backend)
        with self.assertRaises(Blocked):
            adapter.call(None, {}, "stage")
    def test_hostile_prompt_is_data_not_shell(self):
        hostile = "'; $(touch /tmp/injected); `echo injected`"
        argv = wsl_argv("Ubuntu", "/home/user/repo", ["python3", "-c", "import sys; print(sys.argv[1])", hostile])
        self.assertEqual(hostile, argv[-1])
        self.assertNotIn("bash", argv)

    @unittest.skipUnless(os.name == "nt" and shutil.which("wsl.exe"), "WSL host required for native protocol smoke test")
    def test_native_wsl_fake_red_green_commit_transfer_and_recovery(self):
        scenario = (ROOT / "tests/wsl_fake_scenario.py").read_text(encoding="utf-8")
        bridge = (ROOT / "autodispatch/wsl_bridge.py").read_text(encoding="utf-8")
        result = subprocess.run(["wsl.exe", "--distribution", "Ubuntu", "--exec", "python3", "-c", scenario],
                                input=json.dumps({"source": bridge}).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(0, result.returncode, result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace"))
        data = json.loads(result.stdout.decode().splitlines()[-1])
        self.assertTrue(data["simulation"])
        self.assertEqual(0, data["real_agent_calls"])
        self.assertEqual(1, data["red_exit"])
        self.assertEqual(0, data["green_exit"])
        self.assertNotEqual(data["red_sha"], data["green_sha"])
        self.assertTrue(data["bridge_death_stops_native_child"])

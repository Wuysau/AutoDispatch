import unittest
from pathlib import Path
from unittest.mock import patch

from autodispatch.codex_policy import exec_argv, platform_overrides
from autodispatch.model import Blocked
import json


class CodexPolicyTests(unittest.TestCase):
    def setUp(self):
        self.c = {"codex_command": ["node.exe", "codex.js"], "codex_model": "configured-model", "codex_windows_sandbox": "unelevated"}

    def test_review_keeps_read_only_and_explicit_windows_backend(self):
        with patch("autodispatch.codex_policy.os.name", "nt"):
            argv = exec_argv(self.c, Path.cwd(), "read-only", Path("result.json"), Path("schema.json"))
        self.assertIn('windows.sandbox="unelevated"', argv)
        self.assertIn('approval_policy="never"', argv)
        self.assertEqual("read-only", argv[argv.index("--sandbox") + 1])
        self.assertEqual(1, argv.count("--model"))
        self.assertIn("--ignore-user-config", argv)
        self.assertNotIn("--ignore-rules", argv)
        self.assertNotIn("--approve-for-me", argv)

    def test_implementation_keeps_workspace_write_and_same_platform_policy(self):
        with patch("autodispatch.codex_policy.os.name", "nt"):
            argv = exec_argv(self.c, Path.cwd(), "workspace-write", Path("result.txt"))
        self.assertEqual("workspace-write", argv[argv.index("--sandbox") + 1])
        self.assertIn('windows.sandbox="unelevated"', argv)

    def test_unknown_backend_or_unrestricted_mode_fails_closed(self):
        with patch("autodispatch.codex_policy.os.name", "nt"), self.assertRaises(Blocked):
            platform_overrides(dict(self.c, codex_windows_sandbox="disabled"))
        with self.assertRaises(Blocked):
            exec_argv(self.c, Path.cwd(), "danger-full-access", Path("out"))

    def test_reasoning_effort_is_passed_without_changing_sandbox(self):
        argv = exec_argv(dict(self.c, codex_reasoning_effort='high'), Path.cwd(), 'workspace-write', Path('out'))
        self.assertIn('model_reasoning_effort="high"', argv)
        self.assertEqual('configured-model', argv[argv.index('--model') + 1])
        self.assertEqual('workspace-write', argv[argv.index('--sandbox') + 1])

    def test_approved_report_must_not_silently_drop_findings(self):
        from autodispatch.safety import assert_review
        from types import SimpleNamespace
        sha = "a" * 40
        report = {"task_id": "IP-00", "status": "APPROVED", "findings": ["a note"], "commit_sha": sha, "verified_commands": []}
        with self.assertRaisesRegex(Blocked, "protocol violation"):
            assert_review(report, SimpleNamespace(id="IP-00"), sha)
        schema = json.loads((Path(__file__).resolve().parents[1] / "schemas/review.schema.json").read_text())
        self.assertIn("MUST be []", schema["properties"]["findings"]["description"])

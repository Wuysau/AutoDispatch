import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from autodispatch.gitops import GitHub
from autodispatch.model import Blocked
from autodispatch.process import Runner


class ReviewRegressions(unittest.TestCase):
    def test_required_ci_auth_error_cannot_be_empty_checks(self):
        github = GitHub(Runner(), Path.cwd())
        with patch.object(github, "call", side_effect=Blocked("authentication failed")):
            with self.assertRaises(Blocked):
                github.required()

    def test_required_ci_config_is_additional_not_replacement(self):
        github = GitHub(Runner(), Path.cwd(), configured_checks=["local-policy"])
        responses = [{"protected": True}, [], {"required_status_checks": {"contexts": ["remote-ci"]}}]
        with patch.object(github, "call", side_effect=responses):
            self.assertEqual(["local-policy", "remote-ci"], github.required())

    def test_no_ci_needs_successful_branch_and_rules_queries(self):
        github = GitHub(Runner(), Path.cwd())
        with patch.object(github, "call", side_effect=[{"protected": False}, []]):
            self.assertEqual([], github.required())
        with patch.object(github, "call", side_effect=[{"protected": False}, Blocked("rules unavailable")]):
            with self.assertRaises(Blocked):
                github.required()

    def test_fast_child_cannot_outlive_parent_process_tree(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            marker = root / "escaped.txt"
            child = "import time,pathlib; time.sleep(0.7); pathlib.Path(" + repr(str(marker)) + ").write_text('escaped')"
            parent = "import subprocess,sys; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); print('parent exit')"
            result = Runner().run([sys.executable, "-c", parent], root, 5)
            self.assertEqual(0, result.code)
            time.sleep(1)
            self.assertFalse(marker.exists(), "child escaped the parent's process job/group")

    @unittest.skipUnless(os.name == "nt", "Windows job gate regression")
    def test_failed_job_attachment_never_starts_requested_executable(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            marker = root / "should-not-start.txt"
            code = "import pathlib; pathlib.Path(" + repr(str(marker)) + ").write_text('started')"
            with patch("autodispatch.process.ProcessTree", side_effect=Blocked("job setup denied")):
                with self.assertRaises(Blocked):
                    Runner().run([sys.executable, "-c", code], root, 5)
            self.assertFalse(marker.exists())

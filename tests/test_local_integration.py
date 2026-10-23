import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from autodispatch.adapters import RealBackend
from autodispatch.model import Plan, load_config
from autodispatch.store import Store

ROOT = Path(__file__).resolve().parents[1]


class LocalIntegrationTests(unittest.TestCase):
    def test_reviewed_task_merges_local_dev_without_network_and_preserves_spec(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            c = load_config(ROOT / "config.example.json")
            source = Path(c["repo"])
            repo = root / "repo"
            repo.mkdir()
            for name in c["pins"]:
                target = repo / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / name, target)
            shutil.copytree(source / "docs/spec", repo / "docs/spec", dirs_exist_ok=True)
            shutil.copyfile(source / "AGENTS.md", repo / "AGENTS.md")
            c.update(repo=str(repo), state_dir=str(root / "state"), worktree_root=str(root / "worktrees"),
                     integration_target="local-dev", publication_mode="deferred")
            store = Store(Path(c["state_dir"]))
            try:
                plan = Plan(repo, c["pins"])
                backend = RealBackend(c, plan, store)
                git = backend.git
                git.text(repo, "init", "-b", "dev")
                git.text(repo, "config", "user.name", "Offline Fake")
                git.text(repo, "config", "user.email", "offline@example.invalid")
                git.text(repo, "remote", "add", "origin", c["remote_url"])
                git.text(repo, "add", "--", ".")
                git.text(repo, "commit", "-m", "fixture")
                base = backend.current_base()
                task = plan.tasks["IP-35"]
                store.update(task.id, "RUNNING", deadline=time.time() + 60)
                work = backend.prepare(task, base)
                path = Path(work["worktree"]) / "AGENTS.md"
                path.write_bytes(path.read_bytes() + b"\nLocal test addition.\n")
                git.text(path.parent, "add", "--", "AGENTS.md")
                git.text(path.parent, "commit", "-m", "reviewed task fixture")
                sha = git.sha(path.parent)
                review = {"task_id": task.id, "status": "APPROVED", "findings": [], "commit_sha": sha, "verified_commands": []}
                fake_evidence = {"commit_sha": sha, "exit_code": 0,
                                 "stats": {"tests": 1, "failures": 0, "errors": 0, "skipped": 0, "environment_errors": 0}}
                with patch.object(backend, "verify", return_value=fake_evidence) as verify, patch.object(backend.github, "call", side_effect=AssertionError("No GitHub call allowed")):
                    result = backend.integrate_local(task, work, sha, fake_evidence, review)
                merged = git.sha(repo)
                self.assertEqual(merged, result["merge_sha"])
                self.assertTrue(git.ancestor(repo, sha, merged))
                self.assertIn(b"Local test addition", (repo / "AGENTS.md").read_bytes())
                self.assertFalse((Path(c["state_dir"]) / "local-merge-pending.json").exists())
                verify.assert_called_once_with(task, merged, "green")

                # A normal concurrent merge marker must wait for its owning lock,
                # rather than be mistaken for a crash by the scheduling thread.
                marker = Path(c["state_dir"]) / "local-merge-pending.json"
                entered = threading.Event()
                def integrating():
                    with backend.integration_lock:
                        marker.write_text("{}", encoding="utf-8")
                        entered.set()
                        time.sleep(0.15)
                        marker.unlink()
                thread = threading.Thread(target=integrating)
                thread.start()
                self.assertTrue(entered.wait(2))
                self.assertEqual(merged, backend.current_base())
                thread.join()
            finally:
                store.close()

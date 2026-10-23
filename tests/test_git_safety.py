import dataclasses
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from autodispatch.gitops import Git
from autodispatch.model import Blocked, GRAPH, Plan, load_config, validate_spec
from autodispatch.process import Runner
from autodispatch.safety import check_scope

ROOT = Path(__file__).resolve().parents[1]


class GitSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = load_config(ROOT / "config.example.json")
        cls.plan = Plan(Path(cls.c["repo"]), cls.c["pins"])

    def test_crlf_is_not_trailing_whitespace_and_actual_spaces_still_fail(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            git = Git(Runner())
            git.text(repo, 'init', '-b', 'dev')
            git.text(repo, 'config', 'user.email', 'test@example.invalid')
            git.text(repo, 'config', 'user.name', 'CRLF regression')
            target = repo / 'generated.json'
            target.write_bytes(b'{"value": 1}\n')
            git.text(repo, 'add', '--', 'generated.json')
            git.text(repo, 'commit', '-m', 'baseline')
            crlf = b'{\r\n  "value": 2\r\n}\r\n'
            target.write_bytes(crlf)
            self.assertEqual(0, git.run(repo, 'diff', '--check', 'HEAD').code)
            self.assertEqual(crlf, target.read_bytes())
            git.text(repo, 'add', '--', 'generated.json')
            git.text(repo, 'commit', '-m', 'CRLF generated output')
            self.assertEqual(0, git.run(repo, 'diff', '--check', 'HEAD^...HEAD').code)
            target.write_bytes(b'{\r\n  "value": 3  \r\n}\r\n')
            self.assertNotEqual(0, git.run(repo, 'diff', '--check', 'HEAD').code)

    def test_real_disposable_git_commit_worktree_and_lf(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = root / "repo"
            repo.mkdir()
            git = Git(Runner())
            git.text(repo, "init", "-b", "dev")
            git.text(repo, "config", "user.name", "Offline Test")
            git.text(repo, "config", "user.email", "offline@example.invalid")
            original = b"approved\nLF\n"
            (repo / "original.txt").write_bytes(original)
            git.text(repo, "add", "--", "original.txt")
            git.text(repo, "commit", "-m", "fixture")
            base = git.sha(repo)
            git.text(repo, "update-ref", "refs/remotes/origin/dev", base)
            work = root / "isolated"
            git.create(repo, work, base, "codex/test")
            self.assertEqual(original, (work / "original.txt").read_bytes())
            (work / "new.py").write_text("value = 42\n", encoding="utf-8", newline="\n")
            with self.assertRaises(Blocked):
                git.clean(work)
            git.text(work, "add", "--", "new.py")
            git.text(work, "commit", "-m", "behavior")
            sha = git.sha(work)
            self.assertNotEqual(base, sha)
            self.assertTrue(git.ancestor(repo, base, sha))
            self.assertEqual(["new.py"], git.changed(work, base))
            self.assertEqual(0, git.run(work, "diff", "--check", "origin/dev...HEAD").code)
            self.assertEqual(base, git.sha(repo))

    def test_scope_spec_and_credentials_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            task = self.plan.tasks["IP-05"]
            for changed in (["src/another_task.py"], ["docs/spec/approved/original.md"], [".env"]):
                with self.subTest(changed=changed), self.assertRaises(Blocked):
                    check_scope(task, changed, root)
            target = root / task.primary_test_file
            target.parent.mkdir(parents=True)
            target.write_text('api_key="not-for-publication"', encoding="utf-8")
            with self.assertRaises(Blocked):
                check_scope(task, [task.primary_test_file], root)

    def test_domain_authorization_annotations_are_not_mistaken_for_credentials(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            task = self.plan.tasks["IP-05"]
            target = root / task.primary_test_file
            target.parent.mkdir(parents=True)
            target.write_text("authorization: Authorization\nsecret = supplied_value\n", encoding="utf-8")
            check_scope(task, [task.primary_test_file], root)

    def test_spec_modified_bytes_or_manifest_never_pass(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            shutil.copytree(Path(self.c["repo"]) / "docs/spec", root / "docs/spec")
            validate_spec(root, self.c["manifest_sha256"])
            file = next((root / "docs/spec/approved").rglob("*.json"))
            file.write_bytes(file.read_bytes() + b"\n")
            with self.assertRaises(Blocked):
                validate_spec(root, self.c["manifest_sha256"])

    def test_live_configuration_is_rejected(self):
        c = {k: v for k, v in self.c.items() if k != "config_dir"}
        c["live_authorized"] = True
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(json.dumps(c), encoding="utf-8")
            with self.assertRaises(Blocked):
                load_config(path)

    def test_graph_cycles_unknown_dependencies_and_reassignment(self):
        source = json.loads((Path(self.c["repo"]) / GRAPH).read_text(encoding="utf-8"))
        for kind in ("cycle", "unknown", "agent", "count"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                data = json.loads(json.dumps(source))
                if kind == "cycle":
                    data["tasks"][34]["dependencies"] = ["IP-00"]
                elif kind == "unknown":
                    data["tasks"][0]["dependencies"] = ["IP-99"]
                elif kind == "agent":
                    data["tasks"][6]["agent"] = "OpenCode"
                else:
                    data["tasks"].pop()
                for task in data["tasks"]:
                    path = root / task["task_card"]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("fixture", encoding="utf-8")
                (root / GRAPH).write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(Blocked):
                    Plan(root)

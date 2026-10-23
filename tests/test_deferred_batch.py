import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autodispatch.adapters import RealBackend
from autodispatch.batch import BatchBackend, BatchEngine, INTEGRATED, batch_approval, final_checks
from autodispatch.model import Blocked, Plan, load_config
from autodispatch.process import Result
from autodispatch.safety import approval_fingerprint
from autodispatch.store import Store

ROOT = Path(__file__).resolve().parents[1]


class DeferredBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.c = load_config(ROOT / 'config.example.json')
        source = Path(self.c['repo'])
        repo = self.root / 'repo'
        repo.mkdir()
        for name in self.c['pins']:
            target = repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, target)
        shutil.copytree(source / 'docs/spec', repo / 'docs/spec', dirs_exist_ok=True)
        shutil.copyfile(source / 'AGENTS.md', repo / 'AGENTS.md')
        self.c.update(repo=str(repo), state_dir=str(self.root / 'state'),
                      worktree_root=str(self.root / 'trees'), integration_target='local-dev',
                      publication_mode='deferred', opencode_environment='windows')
        self.plan = Plan(repo, self.c['pins'])
        self.store = Store(Path(self.c['state_dir']))
        self.backend = BatchBackend(self.c, self.plan, self.store)
        self.backend.batch_id = 'test-batch'
        self.backend.request = {'user_request': 'merge first, validate last'}
        git = self.backend.git
        git.text(repo, 'init', '-b', 'dev')
        git.text(repo, 'config', 'user.email', 'test@example.invalid')
        git.text(repo, 'config', 'user.name', 'Batch test')
        git.text(repo, 'remote', 'add', 'origin', self.c['remote_url'])
        git.text(repo, 'add', '--', '.')
        git.text(repo, 'commit', '-m', 'baseline')
        self.base = git.sha(repo)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def code(self, tid):
        task = self.plan.tasks[tid]
        work = self.backend.prepare(task, self.backend.current_base())
        self.store.update(tid, **work)
        path = Path(work['worktree'])
        target = path / task.primary_test_file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('def test_real_behavior():\n    assert True\n', newline='\n')
        self.backend.git.text(path, 'add', '--', task.primary_test_file)
        self.backend.git.text(path, 'commit', '-m', 'test implementation')
        return task, work, self.backend.git.sha(path)

    def test_integrates_without_task_tests_or_review_and_records_unverified(self):
        task, work, sha = self.code('IP-01')
        with patch.object(self.backend, 'verify', side_effect=AssertionError('no task verify')):
            merged = self.backend.integrate_unverified(task, work, sha)
        record = self.store.records()[task.id]
        self.assertEqual(INTEGRATED, record['state'])
        self.assertNotIn('green', record)
        self.assertNotIn('review', record)
        self.assertTrue(self.backend.git.ancestor(self.backend.repo, sha, merged))
        self.assertTrue(self.backend.dependency_valid(record, merged))
        self.assertFalse(RealBackend.dependency_valid(self.backend, record, merged))
        record['batch_id'] = 'other-batch'
        self.assertFalse(self.backend.dependency_valid(record, merged))

    def test_normal_workflow_cannot_enter_active_batch(self):
        (self.backend.root / 'batch-active.json').write_text('{}')
        with self.assertRaisesRegex(Blocked, 'batch owns'):
            RealBackend._current_base(self.backend)
        self.assertEqual(self.base, self.backend.current_base())

    def test_reuses_commits_and_merges_current_dependency_base_without_reset(self):
        task, work, old = self.code('IP-01')
        self.store.update(task.id, 'BLOCKED', commit_sha=old)
        repo = self.backend.repo
        (repo / 'dependency.txt').write_text('new dependency')
        self.backend.git.text(repo, 'add', '--', 'dependency.txt')
        self.backend.git.text(repo, 'commit', '-m', 'dependency')
        base = self.backend.current_base()
        adopted = self.backend.prepare(task, base)
        head = self.backend.git.sha(Path(adopted['worktree']))
        self.assertTrue(self.backend.git.ancestor(repo, old, head))
        self.assertTrue(self.backend.git.ancestor(repo, base, head))
        self.assertEqual(base, adopted['base_sha'])

    def test_zero_exit_blocked_agent_is_not_committed_or_integrated(self):
        for tid in ['IP-00', 'IP-35']:
            self.store.update(tid, 'MERGED_VERIFIED')
        task = self.plan.tasks['IP-01']
        work = self.backend.prepare(task, self.base)
        path = Path(work['worktree'])
        def run(argv, *args, **kwargs):
            output = Path(argv[argv.index('--output-last-message') + 1])
            output.write_text('BLOCKED: a real unresolved contract problem')
            return Result(0, '', {})
        with patch.object(self.backend.runner, 'run', side_effect=run):
            with self.assertRaisesRegex(Blocked, 'did not return CODE_READY'):
                self.backend.run_agent(task, 'batch-implement', work)
        self.assertEqual(self.base, self.backend.git.sha(path))
        self.assertEqual(self.base, self.backend.current_base())

    def test_engine_never_calls_task_verification_and_continues_after_integration(self):
        calls = []
        self.backend.dependency_valid = lambda record, base: record.get('state') in {'MERGED_VERIFIED', INTEGRATED}
        for tid in ['IP-00', 'IP-35']:
            self.store.update(tid, 'MERGED_VERIFIED')
        def implementation(task, stage, work):
            calls.append(stage)
            path = Path(work['worktree'])
            target = path / task.primary_test_file
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('def test_result():\n    assert True\n', newline='\n')
            self.backend.git.text(path, 'add', '--', task.primary_test_file)
            self.backend.git.text(path, 'commit', '-m', 'implementation')
            return self.backend.git.sha(path)
        with patch.object(self.backend, 'run_agent', side_effect=implementation), \
             patch.object(self.backend, 'verify', side_effect=AssertionError('verification deferred')):
            result = BatchEngine(self.plan, self.store, self.backend, ['IP-01', 'IP-02']).run(2, 2)
        self.assertEqual(2, result['launched_tasks'])
        self.assertEqual(['batch-implement', 'batch-implement'], calls)
        self.assertEqual({INTEGRATED}, {self.store.records()[tid]['state'] for tid in ['IP-01', 'IP-02']})

    def test_final_checks_use_same_sha_continue_after_failure_and_do_not_certify_tasks(self):
        calls = []
        def run(argv, cwd, timeout, **kwargs):
            calls.append((kwargs['stage'], str(cwd)))
            for arg in argv:
                if arg.startswith('--junitxml='):
                    Path(arg.split('=', 1)[1]).write_text('<testsuite><testcase name="x"/></testsuite>')
            code = 1 if kwargs['stage'] == 'final-IP-01' else 0
            return Result(code, '', {'exit_code': code})
        with patch.object(self.backend.runner, 'run', side_effect=run):
            path, result = final_checks(self.backend, self.plan, ['IP-01', 'IP-02'])
        self.assertEqual('FAILED', result['status'])
        self.assertIn('final-IP-02', [x[0] for x in calls])
        self.assertIn('final-full-pytest', [x[0] for x in calls])
        self.assertEqual(1, len({x[1] for x in calls}))
        self.assertEqual(self.base, result['commit_sha'])
        self.assertTrue(path.exists())
        self.assertEqual({}, self.store.records())

    def test_batch_mode_requires_separate_configuration_bound_authorization(self):
        request = self.root / 'request.json'
        obj = {'workflow': 'integrate-first-validate-last', 'approved_at': '2026-10-22',
               'configuration_sha256': approval_fingerprint(self.c), 'user_request': 'explicit',
               'allow_unverified_local_integration': True, 'authorized_tasks': ['IP-01']}
        request.write_text(json.dumps(obj))
        approved = {'authorized_tasks': ['IP-01']}
        self.assertEqual(obj, batch_approval(self.c, request, approved))
        obj['allow_unverified_local_integration'] = False
        request.write_text(json.dumps(obj))
        with self.assertRaises(Blocked):
            batch_approval(self.c, request, approved)


if __name__ == '__main__':
    unittest.main()

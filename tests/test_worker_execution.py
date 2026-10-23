import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from autodispatch.adapters import RealBackend
from autodispatch.model import Blocked
from autodispatch.process import Result
from autodispatch.store import Store
from autodispatch.workers import reserve_worker, save_profile
from autodispatch.wsl_adapter import WslAdapter


class WorkerExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.c = {'codex_command': ['codex.exe'], 'opencode_command': ['opencode.exe'],
                  'codex_windows_sandbox': 'unelevated', 'opencode_environment': 'windows',
                  'codex_model': 'base-codex', 'opencode_model': 'provider/base',
                  'worker_model_selection': 'official'}
        catalog = patch('autodispatch.model_catalog.catalog', return_value={'status': 'ready', 'models': [
            {'model': 'selected-codex', 'reasoningEfforts': [None, 'new-effort']},
            {'model': 'provider/selected', 'reasoningEfforts': [None, 'deep-variant']}]})
        catalog.start()
        self.addCleanup(catalog.stop)
        self.backend = object.__new__(RealBackend)
        self.backend.c, self.backend.store, self.backend.root = self.c, self.store, self.root
        self.backend.repo = self.root
        self.backend.git = SimpleNamespace(clean=Mock(), sha=Mock(return_value='base'))
        self.backend.agent_context = lambda *args: 'fixture task'
        self.backend.remaining = lambda task: 10
        self.backend.runner = SimpleNamespace(run=Mock(side_effect=Blocked('captured')))
        self.work = {'worktree': str(self.root), 'base_sha': 'base'}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def prepare(self, executor, model, effort):
        task = SimpleNamespace(id='IP-01', agent=executor, scope_write=(), primary_test_file='test.py', test_support_file=None)
        save_profile(self.store, self.c, 1, executor, {'model': model, 'reasoningEffort': effort})
        reserve_worker(self.store, self.c, task, 1, 'base')
        return task

    def test_codex_invocation_uses_pinned_model_and_effort(self):
        task = self.prepare('Codex', 'selected-codex', 'new-effort')
        save_profile(self.store, self.c, 1, 'Codex', {'model': 'base-codex', 'reasoningEffort': None})
        with self.assertRaisesRegex(Blocked, 'captured'):
            self.backend.run_agent(task, 'implement', self.work)
        args, kwargs = self.backend.runner.run.call_args
        self.assertEqual('selected-codex', args[0][args[0].index('--model') + 1])
        self.assertIn('model_reasoning_effort="new-effort"', args[0])
        self.assertEqual('new-effort', kwargs['reasoning_effort'])
        self.assertEqual('selected-codex', kwargs['model'])
        self.assertEqual('base-codex', self.c['codex_model'])

    def test_opencode_uses_official_model_variant_selector(self):
        task = self.prepare('OpenCode', 'provider/selected', 'deep-variant')
        with self.assertRaisesRegex(Blocked, 'captured'):
            self.backend.run_agent(task, 'implement', self.work)
        args, kwargs = self.backend.runner.run.call_args
        self.assertEqual('provider/selected#deep-variant', args[0][args[0].index('--model') + 1])
        self.assertNotIn('--variant', args[0])
        self.assertEqual('deep-variant', kwargs['reasoning_effort'])

    def test_opencode_task_does_not_change_independent_codex_review_model(self):
        task = self.prepare('OpenCode', 'provider/selected', 'deep-variant')
        self.backend.plan = SimpleNamespace()
        with patch('autodispatch.adapters.context', return_value='fixture review'), self.assertRaisesRegex(Blocked, 'captured'):
            self.backend.review(task, 'base', self.root, {})
        args, kwargs = self.backend.runner.run.call_args
        self.assertEqual('base-codex', args[0][args[0].index('--model') + 1])
        self.assertEqual('read-only', args[0][args[0].index('--sandbox') + 1])
        self.assertIsNone(kwargs['reasoning_effort'])

    def test_wsl_handoff_uses_the_same_pinned_selector(self):
        self.c.update(integration_target='origin-dev', remote_url='fixture', pins={}, manifest_sha256='fixture',
                      opencode_environment='wsl', wsl={'distribution': 'Ubuntu', 'repo': '/home/user/repo',
                      'worktree_root': '/home/user/trees', 'state_dir': '/home/user/state', 'opencode_command': ['/usr/bin/opencode']})
        task = self.prepare('OpenCode', 'provider/selected', 'deep-variant')
        captured = {}
        def run(*args, **kwargs):
            payload = json.loads(kwargs['stdin'])
            captured.update(payload)
            return Result(0, json.dumps({'autodispatch_bridge': payload['nonce'], 'result': {}}), {})
        self.backend.runner.run.side_effect = run
        WslAdapter(self.backend).call(task, self.work, 'stage', 'fixture prompt', 'implement')
        self.assertEqual('provider/selected#deep-variant', captured['model'])
        self.assertEqual('provider/selected', self.backend.runner.run.call_args.kwargs['model'])
        self.assertEqual('deep-variant', self.backend.runner.run.call_args.kwargs['reasoning_effort'])

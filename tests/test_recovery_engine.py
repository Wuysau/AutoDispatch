import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_deferred_batch as fixtures
from autodispatch.model import Blocked
from autodispatch.process import Result
from autodispatch.recovery import RecoveryBackend, RecoveryEngine
from autodispatch.recovery_queue import RecoveryQueue
from autodispatch.recovery_snapshot import capture, restore, classify_failure


class RecoveryEngineTests(unittest.TestCase):
    def setUp(self):
        fixtures.DeferredBatchTests.setUp(self)
        self.backend = RecoveryBackend(self.c, self.plan, self.store)
        self.backend.batch_id = 'test-batch'
        self.backend.request = {'user_request': 'Integrate first, validate last; automatically repair twice'}
        self.queue = RecoveryQueue(self.store, backoff=0)
        self.engine = RecoveryEngine(self.plan, self.store, self.backend, ['IP-01', 'IP-02'], queue=self.queue)
        self.backend.dependency_valid = lambda r, base: r.get('state') in {'MERGED_VERIFIED', 'INTEGRATED_UNVERIFIED'}
        for tid in ['IP-00', 'IP-35']:
            self.store.update(tid, 'MERGED_VERIFIED')

    def tearDown(self):
        fixtures.DeferredBatchTests.tearDown(self)

    def partial(self, tracked=False):
        task = self.plan.tasks['IP-01']
        work = self.backend.prepare(task, self.base)
        path = Path(work['worktree'])
        target = path / task.primary_test_file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'def test_behavior():\r\n    assert True\r\n')
        if tracked:
            self.backend.git.text(path, 'add', '--', task.primary_test_file)
            self.backend.git.text(path, 'commit', '-m', 'partial test')
            target.write_bytes(b'def test_behavior():\r\n    assert 1 == 1\r\n')
        self.store.update(task.id, 'BLOCKED', **work, commit_sha=self.backend.git.sha(path),
                          writer_released=True, reason='Command failed (124): partial implementation timeout',
                          last_receipt={'exit_code': 124, 'timeout': True, 'elapsed_seconds': 600,
                                        'receipt_path': 'original-timeout'})
        return task, work, target

    def test_snapshot_restores_uncommitted_bytes_in_new_worktree_and_keeps_original(self):
        task, work, original = self.partial(tracked=True)
        extra = Path(work['worktree']) / 'src/trading_system/contracts/canonical.py'
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_bytes(b'# partial\r\nVALUE = 42\r\n')
        snapshot = capture(self.backend, task, self.store.records()[task.id])
        restored, data = restore(self.backend, task, snapshot, 1, self.base)
        self.assertNotEqual(restored['worktree'], work['worktree'])
        self.assertEqual(original.read_bytes(), (Path(restored['worktree']) / task.primary_test_file).read_bytes())
        self.assertEqual(extra.read_bytes(), (Path(restored['worktree']) / 'src/trading_system/contracts/canonical.py').read_bytes())
        self.assertEqual('agent', data['kind'])
        self.assertNotIn('environ', data['environment'])
        self.assertEqual(600, data['receipt']['elapsed_seconds'])

    def test_tampered_manifest_is_refused_before_restore(self):
        task, _, _ = self.partial()
        snap = capture(self.backend, task, self.store.records()[task.id])
        manifest = Path(snap['path'])
        manifest.write_bytes(manifest.read_bytes() + b' ')
        with self.assertRaisesRegex(Blocked, 'hash mismatch'):
            restore(self.backend, task, snap, 1, self.base)

    def test_recovery_claims_partial_work_and_integrates_without_running_tests(self):
        task, original_work, original = self.partial()
        self.engine.record_failure(task)
        self.assertEqual('RECOVERY_QUEUED', self.store.records()[task.id]['state'])
        self.assertEqual(['IP-02', 'IP-01'], [t.id for t in self.engine.ready(self.base)])
        calls = []
        def agent(argv, cwd, timeout, **kw):
            calls.append((timeout, kw['stage'], kw['agent']))
            output = Path(argv[argv.index('--output-last-message') + 1])
            output.write_text('CODE_READY_VERIFY_PENDING\nRecovered partial implementation')
            return Result(0, '', {})
        with patch.object(self.backend.runner, 'run', side_effect=agent), \
             patch.object(self.backend, 'verify', side_effect=AssertionError('tests must remain deferred')):
            self.engine.task(task, self.base)
        record = self.store.records()[task.id]
        self.assertEqual('INTEGRATED_UNVERIFIED', record['state'])
        self.assertEqual('RESOLVED', record['recovery']['state'])
        self.assertEqual(1, record['recovery']['attempts'])
        self.assertEqual(1, len(calls))
        self.assertEqual('batch-repair', calls[0][1])
        self.assertGreater(calls[0][0], 600)
        self.assertLessEqual(calls[0][0], 1200)
        self.assertEqual(b'def test_behavior():\r\n    assert True\r\n', original.read_bytes())

    def test_completed_handoff_can_commit_and_integrate_without_another_agent(self):
        task, _, _ = self.partial()
        snap = capture(self.backend, task, self.store.records()[task.id])
        manifest = Path(snap['path'])
        data = json.loads(manifest.read_text(encoding='utf-8'))
        data['kind'] = 'commit'
        from autodispatch.control import atomic_json
        from autodispatch.model import digest
        atomic_json(manifest, data)
        snap['sha256'] = digest(manifest.read_bytes())
        self.queue.enqueue(task.id, snap, 'format failure', error_class='FORMAT')
        with patch.object(self.backend.runner, 'run', side_effect=AssertionError('no extra model call')):
            self.engine.task(task, self.base)
        self.assertEqual('INTEGRATED_UNVERIFIED', self.store.records()[task.id]['state'])

    def test_failed_repair_retains_new_progress_and_exhausts_after_two_attempts(self):
        task, _, _ = self.partial()
        self.engine.record_failure(task)
        for attempt in (1, 2):
            def fail(argv, cwd, timeout, **kw):
                return Result(124, 'timed out again', {})
            with patch.object(self.backend.runner, 'run', side_effect=fail):
                self.engine.task(task, self.backend.current_base())
            job = self.queue.records()[task.id]
            self.assertEqual(attempt, job['attempts'])
        self.assertEqual('EXHAUSTED', job['state'])
        self.assertEqual('RECOVERY_EXHAUSTED', self.store.records()[task.id]['state'])

    def test_process_ambiguity_and_protected_policy_do_not_auto_retry(self):
        self.assertEqual(('AMBIGUOUS_INTEGRATION', False), classify_failure({'local_merge_intent': True}))
        self.assertEqual(('PROTECTED_BOUNDARY', False), classify_failure({'reason': 'Scope expansion forbidden'}))
        self.assertEqual(('USER_STOP', False), classify_failure({'last_receipt': {'stopped': True}}))

    def test_engine_preserves_fifo_and_manual_recovery_priority(self):
        for tid, now in [('IP-02', 100), ('IP-01', 200)]:
            self.queue.enqueue(tid, {'path': 'snapshot', 'sha256': 'hash'}, 'failure', now=now)
        self.assertEqual(['IP-02', 'IP-01'], [task.id for task in self.engine.ready(self.base)])
        self.queue.prioritize('IP-01')
        self.assertEqual(['IP-01', 'IP-02'], [task.id for task in self.engine.ready(self.base)])

    def test_dead_claim_is_recaptured_once_without_resetting_attempts(self):
        task, _, _ = self.partial()
        self.engine.record_failure(task)
        job = self.queue.claim(task.id)
        job['lease_pid'] = 99999999
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE recovery_jobs SET payload=? WHERE task=?', (json.dumps(job), task.id))
        self.engine.recover_interrupted()
        recovered = self.queue.records()[task.id]
        self.assertEqual('QUEUED', recovered['state'])
        self.assertEqual(1, recovered['attempts'])
        self.assertNotEqual(job['snapshot'], recovered['snapshot'])
        self.engine.recover_interrupted()
        self.assertEqual(recovered['snapshot'], self.queue.records()[task.id]['snapshot'])


if __name__ == '__main__':
    unittest.main()

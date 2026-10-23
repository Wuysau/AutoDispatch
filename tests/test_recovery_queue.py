import concurrent.futures
import json
import tempfile
import threading
import unittest
from pathlib import Path

from autodispatch.model import Blocked
from autodispatch.recovery_queue import RecoveryQueue
from autodispatch.store import Store


class RecoveryQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.queue = RecoveryQueue(self.store, max_attempts=2)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def enqueue(self, **kw):
        return self.queue.enqueue('IP-01', {'path': 'snapshot', 'sha256': 'hash'}, 'failure', **kw)

    def test_one_task_has_one_job_and_restart_retains_attempts(self):
        first = self.enqueue(now=100)
        self.enqueue(now=100)
        self.assertEqual(1, len(self.queue.records()))
        claim = self.queue.claim('IP-01', now=100)
        self.queue.release(claim)
        self.enqueue(now=100)
        restarted = RecoveryQueue(self.store)
        job = restarted.records()['IP-01']
        self.assertEqual(first['id'], job['id'])
        self.assertEqual(1, job['attempts'])
        self.assertEqual('RECOVERY_QUEUED', self.store.records()['IP-01']['state'])

    def test_two_connections_cannot_claim_the_same_task(self):
        self.enqueue(now=100)
        second_store = Store(self.root)
        try:
            second = RecoveryQueue(second_store)
            barrier = threading.Barrier(2)
            def claim(q):
                barrier.wait()
                return q.claim('IP-01', now=100)
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                answers = list(pool.map(claim, [self.queue, second]))
            self.assertEqual(1, sum(x is not None for x in answers))
        finally:
            second_store.close()

    def test_backoff_and_two_attempt_cap_never_reset(self):
        self.enqueue(now=100)
        first = self.queue.claim('IP-01', now=100)
        self.queue.release(first)
        self.enqueue(now=100)
        self.assertEqual([], self.queue.ready(now=129))
        second = self.queue.claim('IP-01', now=130)
        self.assertEqual(2, second['attempts'])
        self.queue.release(second)
        self.enqueue(now=130)
        self.assertEqual('EXHAUSTED', self.queue.records()['IP-01']['state'])
        self.assertIsNone(self.queue.claim('IP-01', now=9999))
        self.assertEqual('RECOVERY_EXHAUSTED', self.store.records()['IP-01']['state'])

    def test_receipts_charge_once_and_active_time_is_finite(self):
        self.enqueue(elapsed=1800, receipt_key='one', now=100)
        self.enqueue(elapsed=1800, receipt_key='one', now=100)
        self.assertEqual(1800, self.queue.records()['IP-01']['spent_seconds'])
        self.enqueue(elapsed=1800, receipt_key='two', now=100)
        self.assertEqual('EXHAUSTED', self.queue.records()['IP-01']['state'])

    def test_protected_failure_is_recorded_without_automatic_claim(self):
        self.enqueue(error_class='PROTECTED_BOUNDARY', retryable=False, now=100)
        self.assertEqual('BLOCKED', self.queue.records()['IP-01']['state'])
        self.assertEqual([], self.queue.ready(now=100))

    def test_ambiguous_process_identity_is_preserved_for_inspection(self):
        self.store.update('IP-01', 'RUNNING', pid=123, pending_launch=True)
        self.enqueue(error_class='AMBIGUOUS_PROCESS', retryable=False, now=100)
        record = self.store.records()['IP-01']
        self.assertEqual(123, record['pid'])
        self.assertTrue(record['pending_launch'])

    def test_stale_lease_cannot_release_another_worker_claim(self):
        self.enqueue(now=100)
        claim = self.queue.claim('IP-01', now=100)
        forged = dict(claim, lease_token='wrong')
        with self.assertRaises(Blocked):
            self.queue.release(forged)
        self.queue.release(claim, resolved=True)
        self.assertEqual('RESOLVED', self.store.records()['IP-01']['recovery']['state'])

    def test_fifo_uses_first_arrival_not_backoff_deadline_or_task_id(self):
        self.queue.enqueue('IP-02', {'path': 'snapshot', 'sha256': 'hash'}, 'first', now=100)
        self.enqueue(now=100)
        claim = self.queue.claim('IP-02', now=100)
        self.queue.enqueue('IP-02', claim['snapshot'], 'again', now=200, claimed=claim)
        self.assertEqual(['IP-01'], [j['task_id'] for j in self.queue.ready(now=229)])
        self.assertEqual(['IP-02', 'IP-01'], [j['task_id'] for j in self.queue.ready(now=230)])

    def test_manual_retry_moves_to_front_persists_and_does_not_reset_budgets(self):
        self.enqueue(now=100, elapsed=600, receipt_key='receipt')
        claim = self.queue.claim('IP-01', now=100)
        self.enqueue(now=200, claimed=claim)
        self.queue.enqueue('IP-02', claim['snapshot'], 'second', now=101)
        self.queue.enqueue('IP-03', claim['snapshot'], 'third', now=102)
        self.store.charge(10)
        self.queue.prioritize('IP-03', now=210)
        promoted = self.queue.prioritize('IP-01', now=210)
        self.assertEqual(1, promoted['attempts'])
        self.assertEqual(600, promoted['spent_seconds'])
        self.assertEqual(100, promoted['created_at'])
        self.assertEqual(claim['snapshot'], promoted['snapshot'])
        self.assertEqual(1, self.store.agent_requests_used())
        second = Store(self.root)
        try:
            self.assertEqual(['IP-01', 'IP-03', 'IP-02'],
                             [j['task_id'] for j in RecoveryQueue(second).ready(now=210)])
        finally:
            second.close()
        claimed = self.queue.claim('IP-01', now=210)
        self.assertEqual(0, claimed['manual_priority'])

    def test_manual_priority_is_consumed_by_one_attempt(self):
        self.enqueue(now=100)
        self.queue.enqueue('IP-02', {'path': 'snapshot', 'sha256': 'hash'}, 'second', now=101)
        self.queue.prioritize('IP-02', now=102)
        claim = self.queue.claim('IP-02', now=102)
        self.queue.enqueue('IP-02', claim['snapshot'], 'again', now=102, claimed=claim)
        self.assertEqual(['IP-01', 'IP-02'], [j['task_id'] for j in self.queue.ready(now=200)])

    def test_manual_retry_refuses_unsafe_states_and_retains_records(self):
        self.enqueue(now=100)
        original = self.queue.records()['IP-01']
        for state in ('CLAIMED', 'BLOCKED', 'EXHAUSTED', 'RESOLVED', 'RELEASED'):
            with self.subTest(state=state):
                job = dict(original, state=state)
                with self.store.db:
                    self.store.db.execute('UPDATE recovery_jobs SET payload=? WHERE task=?',
                                          (json.dumps(job), 'IP-01'))
                with self.assertRaises(Blocked):
                    self.queue.prioritize('IP-01', now=101)
                self.assertEqual(job, self.queue.records()['IP-01'])
        for fields in ({'attempts': 2}, {'spent_seconds': 3600}, {'snapshot': None}):
            with self.subTest(fields=fields):
                job = dict(original, **fields)
                with self.store.db:
                    self.store.db.execute('UPDATE recovery_jobs SET payload=? WHERE task=?',
                                          (json.dumps(job), 'IP-01'))
                with self.assertRaises(Blocked):
                    self.queue.prioritize('IP-01', now=101)
        with self.assertRaises(Blocked):
            self.queue.prioritize('IP-99', now=101)

    def test_claim_and_manual_retry_cannot_overwrite_each_other(self):
        self.enqueue(now=100)
        second_store = Store(self.root)
        try:
            second = RecoveryQueue(second_store)
            barrier = threading.Barrier(2)
            def act(action):
                barrier.wait()
                try:
                    return action('IP-01', now=100)
                except Blocked:
                    return None
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                list(pool.map(act, [self.queue.claim, second.prioritize]))
            job = self.queue.records()['IP-01']
            self.assertEqual('CLAIMED', job['state'])
            self.assertEqual(1, job['attempts'])
            self.assertEqual(0, job['manual_priority'])
        finally:
            second_store.close()

    def test_manual_retry_rejects_changed_task_ownership_without_writes(self):
        self.enqueue(now=100)
        baseline = self.store.records()['IP-01']
        for fields in ({'state': 'RUNNING'}, {'pid': 123}, {'pending_launch': True},
                       {'local_merge_intent': True}, {'writer_released': False}):
            with self.subTest(fields=fields):
                rec = dict(baseline, **fields)
                with self.store.db:
                    self.store.db.execute('UPDATE tasks SET payload=? WHERE id=?', (json.dumps(rec), 'IP-01'))
                before = self.store.history('IP-01')
                with self.assertRaises(Blocked):
                    self.queue.prioritize('IP-01', now=101)
                self.assertEqual(before, self.store.history('IP-01'))
                self.assertEqual(rec, self.store.records()['IP-01'])

    def test_legacy_jobs_recover_first_queue_event_order_read_only(self):
        # Initial task creation order is intentionally different from queue arrival.
        self.store.update('IP-01', 'PENDING')
        self.store.update('IP-02', 'PENDING')
        self.queue.enqueue('IP-02', {'path': 'snapshot', 'sha256': 'hash'}, 'first', now=100)
        self.enqueue(now=100)
        for tid, job in self.queue.records().items():
            del job['created_seq']
            with self.store.db:
                self.store.db.execute('UPDATE recovery_jobs SET payload=? WHERE task=?', (json.dumps(job), tid))
        before = list(self.store.db.execute('SELECT task,payload FROM recovery_jobs'))
        self.assertEqual(['IP-02', 'IP-01'], [j['task_id'] for j in self.queue.ready(now=100)])
        self.assertEqual(before, list(self.store.db.execute('SELECT task,payload FROM recovery_jobs')))


if __name__ == '__main__':
    unittest.main()

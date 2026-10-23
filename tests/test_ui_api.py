import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autodispatch.api import make_server
from autodispatch.model import load_config
from autodispatch.recovery_queue import RecoveryQueue
from autodispatch.store import Store
from autodispatch.workers import reserve_worker
from autodispatch.safety import approval_fingerprint

ROOT = Path(__file__).resolve().parents[1]


def fake_plan():
    rows = {
        "IP-00": ("Codex", ()),
        "IP-01": ("Codex", ("IP-00",)),
        "IP-02": ("OpenCode", ("IP-00",)),
        "IP-03": ("Codex", ("IP-01",)),
    }
    tasks = {tid: SimpleNamespace(id=tid, agent=agent, dependencies=deps, title=f"Title {tid}")
             for tid, (agent, deps) in rows.items()}
    return SimpleNamespace(tasks=tasks)


class UiApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.c = load_config(ROOT / "config.example.json")
        self.c.pop('worker_model_selection', None)
        self.c["state_dir"] = str(self.root)
        self.c["max_workers"] = 2
        self.server = make_server(self.c, port=0, plan=fake_plan())
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {"Host": f"127.0.0.1:{self.port}"})
            res = conn.getresponse()
            return res.status, json.loads(res.read() or b"{}")
        finally:
            conn.close()

    def post_json(self, path, payload):
        return self.request("POST", path, json.dumps(payload),
                            {"Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json"})

    def seed(self):
        store = Store(self.root)
        try:
            store.update("IP-00", "MERGED_VERIFIED")
            store.update("IP-01", "RUNNING", executor="Codex", worktree=str(self.root / "wt"))
            store.update("IP-02", "BLOCKED", reason="token=sk-abcdefghijklmnopqrstuv failed")
        finally:
            store.close()

    def test_snapshot_maps_real_states_and_dependencies(self):
        self.seed()
        status, snap = self.request("GET", "/api/autodispatch/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(snap["source"], "live")
        by_id = {t["id"]: t for t in snap["tasks"]}
        self.assertEqual(sorted(by_id), ["IP-00", "IP-01", "IP-02", "IP-03"])
        self.assertEqual(by_id["IP-00"]["status"], "completed")
        self.assertEqual(by_id["IP-01"]["status"], "running")
        self.assertIsNone(by_id["IP-01"]["progress"])
        self.assertEqual(by_id["IP-02"]["status"], "failed")
        self.assertNotIn("sk-abcdefghijklmnopqrstuv", by_id["IP-02"]["lastError"])
        self.assertEqual(by_id["IP-03"]["status"], "pending")
        self.assertEqual(by_id["IP-03"]["blockedBy"], ["IP-01"])
        self.assertEqual(snap["workers"][0]["taskId"], "IP-01")
        self.assertEqual(1, len(snap['workers']))
        self.assertEqual(snap["runState"], "stopped")
        self.assertTrue(snap["logs"])
        self.assertLessEqual(len(snap["logs"]), 200)
        self.assertEqual(['3', '2', '1'], [entry['id'] for entry in snap['logs']])

    def test_unverified_integration_is_waiting_and_can_supply_downstream_code(self):
        store = Store(self.root)
        store.update('IP-00', 'MERGED_VERIFIED')
        store.update('IP-01', 'INTEGRATED_UNVERIFIED', reason='等待批次统一校验')
        store.close()
        status, snap = self.request('GET', '/api/autodispatch/snapshot')
        rows = {t['id']: t for t in snap['tasks']}
        self.assertEqual(200, status)
        self.assertEqual('waiting', rows['IP-01']['status'])
        self.assertIsNone(rows['IP-01']['progress'])
        self.assertEqual('等待批次统一校验', rows['IP-01']['blockedReason'])
        self.assertNotIn('blockedBy', rows['IP-03'])

    def test_queue_is_visible_with_attempts_and_is_not_counted_as_active_worker(self):
        from autodispatch.recovery_queue import RecoveryQueue
        store = Store(self.root)
        store.update('IP-00', 'MERGED_VERIFIED')
        RecoveryQueue(store).enqueue('IP-01', {'path': 'snapshot', 'sha256': 'hash'},
                                     'Timed out after 600 seconds', 'TIMEOUT')
        store.close()
        status, snap = self.request('GET', '/api/autodispatch/snapshot')
        rows = {t['id']: t for t in snap['tasks']}
        self.assertEqual(200, status)
        self.assertEqual('waiting', rows['IP-01']['status'])
        self.assertEqual('QUEUED', rows['IP-01']['recovery']['state'])
        self.assertEqual(2, rows['IP-01']['recovery']['maxAttempts'])
        self.assertTrue(all(w['taskId'] is None for w in snap['workers']))

    def test_idle_snapshot_is_byte_stable_between_polls(self):
        self.seed()
        first = self.request("GET", "/api/autodispatch/snapshot")[1]
        second = self.request("GET", "/api/autodispatch/snapshot")[1]
        self.assertEqual(first, second)

    def test_empty_state_shows_full_plan_as_pending(self):
        status, snap = self.request("GET", "/api/autodispatch/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual({t["status"] for t in snap["tasks"]}, {"pending"})
        self.assertEqual(snap["logs"], [])

    def test_execution_endpoint_is_read_only_and_validates_task_and_cursor(self):
        self.seed()
        before = Store.snapshot(self.root)
        status, detail = self.request('GET', '/api/autodispatch/tasks/IP-01/execution')
        self.assertEqual(200, status)
        self.assertEqual('unavailable', detail['execution']['telemetry'])
        self.assertEqual([], detail['events'])
        for query in ['after=-1', 'after=abc', 'after=9999999999999999999999']:
            self.assertEqual(400, self.request('GET', '/api/autodispatch/tasks/IP-01/execution?' + query)[0])
        self.assertEqual(404, self.request('GET', '/api/autodispatch/tasks/IP-99/execution')[0])
        self.assertEqual(before, Store.snapshot(self.root))
        self.assertFalse((self.root / 'executions').exists())

    def test_execution_events_are_live_even_when_database_signature_does_not_change(self):
        import time
        from autodispatch.execution import ExecutionTrace
        trace = ExecutionTrace(self.root, 'IP-01', 'machine-green', time.time())
        store = Store(self.root)
        store.update('IP-01', 'RUNNING', execution_id=trace.id, stage='machine-green')
        store.close()
        first = self.request('GET', '/api/autodispatch/tasks/IP-01/execution')[1]
        trace.feed(b'new tool output\n')
        status, second = self.request('GET', f"/api/autodispatch/tasks/IP-01/execution?after={first['cursor']}&attempt={trace.id}")
        self.assertEqual(200, status)
        self.assertEqual(['new tool output'], [row['message'] for row in second['events']])
        self.assertEqual('new tool output', second['execution']['currentOperation'])
        snap = self.request('GET', '/api/autodispatch/snapshot')[1]
        self.assertEqual('machine-green', next(t for t in snap['tasks'] if t['id'] == 'IP-01')['stage'])

    def test_logs_are_latest_first_with_sequence_tiebreak(self):
        store = Store(self.root)
        try:
            for at in (300, 100, 300):
                with store.db:
                    store.db.execute('INSERT INTO events(task,at,payload) VALUES(?,?,?)',
                                     ('IP-01', at, json.dumps({'state': 'BLOCKED'})))
        finally:
            store.close()
        _, snap = self.request('GET', '/api/autodispatch/snapshot')
        self.assertEqual(['3', '1', '2'], [entry['id'] for entry in snap['logs']])

    def test_stop_writes_request_and_pause_resume_are_refused(self):
        status, body = self.post_json("/api/autodispatch/control", {"action": "stop"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "STOP_REQUESTED")
        self.assertTrue((self.root / "stop.request").exists())
        status, body = self.post_json("/api/autodispatch/control", {"action": "pause"})
        self.assertEqual(status, 409)
        self.assertIn("error", body)
        status, _ = self.post_json("/api/autodispatch/control", {"action": "explode"})
        self.assertEqual(status, 400)

    def test_retry_without_recovery_job_is_refused_and_unknown_ids_are_404(self):
        status, body = self.post_json("/api/autodispatch/tasks/IP-02/retry", {})
        self.assertEqual(status, 409)
        self.assertIn("恢复队列", body["error"])
        status, _ = self.post_json("/api/autodispatch/tasks/IP-99/retry", {})
        self.assertEqual(status, 404)

    def test_worker_choices_are_persisted_for_next_assignment_without_live_changes(self):
        self.c.update(max_workers='auto', codex_model='model-a', worker_model_selection='official')
        catalog_result = {'status': 'ready', 'source': 'Official CLI fixture', 'models': [
            {'model': 'model-a', 'reasoningEfforts': [None, 'low', 'high']},
            {'model': 'model-b', 'reasoningEfforts': [None, 'medium']}]}
        for name in ('autodispatch.model_catalog.catalog', 'autodispatch.api.catalog'):
            mocked = patch(name, return_value=catalog_result)
            mocked.start()
            self.addCleanup(mocked.stop)
        store = Store(self.root)
        try:
            reserve_worker(store, self.c, fake_plan().tasks['IP-01'], 2, 'base')
            reserve_worker(store, self.c, fake_plan().tasks['IP-02'], 1, 'base')
            store.update('IP-02', reason='new event must not change slot identity')
            _, snap = self.request('GET', '/api/autodispatch/snapshot')
            self.assertEqual(2, snap['concurrency'])
            self.assertEqual('auto', snap['workerMode'])
            self.assertEqual([(1, 'IP-02'), (2, 'IP-01')], [(w['slot'], w['taskId']) for w in snap['workers']])
            store.charge(10)
            (self.root / 'stop.request').write_text('keep stop')
            status, result = self.post_json('/api/autodispatch/workers/2/settings', {
                'executor': 'Codex', 'model': 'model-b', 'reasoningEffort': 'medium'})
            self.assertEqual(200, status)
            self.assertEqual('SAVED', result['status'])
            _, snap = self.request('GET', '/api/autodispatch/snapshot')
            worker = snap['workers'][1]
            self.assertEqual('model-a', worker['profile']['model'])
            self.assertEqual('model-b', worker['nextProfile']['model'])
            self.assertEqual(1, store.agent_requests_used())
            self.assertEqual('keep stop', (self.root / 'stop.request').read_text())
            for body in ({'executor': 'Codex', 'model': 'model-b', 'reasoningEffort': 'high'},
                         {'executor': [], 'model': 'model-a', 'reasoningEffort': None}):
                self.assertEqual(400, self.post_json('/api/autodispatch/workers/2/settings', body)[0])
            self.assertEqual(400, self.post_json('/api/autodispatch/workers/9/settings', {
                'executor': 'Codex', 'model': 'model-a', 'reasoningEffort': None})[0])
            store.update('IP-01', 'MERGED_VERIFIED', worker_active=False)
            _, snap = self.request('GET', '/api/autodispatch/snapshot')
            self.assertEqual([1], [w['slot'] for w in snap['workers']])
        finally:
            store.close()

    def test_dead_coordinator_does_not_keep_historical_worker_active(self):
        store = Store(self.root)
        try:
            store.update('IP-01', 'RUNNING', worker_slot=1, worker_active=True, coordinator_pid=99999999)
        finally:
            store.close()
        _, snap = self.request('GET', '/api/autodispatch/snapshot')
        self.assertEqual([], snap['workers'])

    def test_snapshot_and_settings_honor_cli_reduced_capacity(self):
        self.c['max_workers'] = 'auto'
        store = Store(self.root)
        try:
            with store.db:
                store.db.execute('INSERT INTO meta VALUES(?,?)', ('scheduler', json.dumps({
                    'configuration_sha256': approval_fingerprint(self.c), 'capacity': 1,
                    'mode': 1, 'authorized_tasks': list(fake_plan().tasks)})))
            _, snap = self.request('GET', '/api/autodispatch/snapshot')
            self.assertEqual(1, snap['concurrency'])
            self.assertEqual(2, snap['dagMaxParallel'])
            self.assertEqual('limited', snap['workerMode'])
            self.assertEqual(400, self.post_json('/api/autodispatch/workers/2/settings', {
                'executor': 'Codex', 'model': None, 'reasoningEffort': None})[0])
        finally:
            store.close()

    def test_retry_promotes_existing_job_without_starting_or_resetting(self):
        store = Store(self.root)
        try:
            queue = RecoveryQueue(store)
            queue.enqueue('IP-02', {'path': 'snapshot', 'sha256': 'hash'}, 'first', now=100)
            queue.enqueue('IP-01', {'path': 'snapshot', 'sha256': 'hash'}, 'second', now=200)
            store.charge(10)
            (self.root / 'stop.request').write_text('preserve stop')
            status, snap = self.request('GET', '/api/autodispatch/snapshot')
            recovery = {t['id']: t for t in snap['tasks']}['IP-02']['recovery']
            self.assertEqual(100000, recovery['enqueuedAt'])
            self.assertTrue(recovery['canRetry'])
            status, body = self.post_json('/api/autodispatch/tasks/IP-01/retry', {})
            self.assertEqual(200, status)
            self.assertEqual('QUEUED', body['status'])
            self.assertEqual(['IP-01', 'IP-02'], [j['task_id'] for j in queue.ready()])
            self.assertEqual(0, queue.records()['IP-01']['attempts'])
            self.assertEqual(1, store.agent_requests_used())
            self.assertEqual('preserve stop', (self.root / 'stop.request').read_text())
            self.assertFalse((self.root / 'service.json').exists())
            _, snap = self.request('GET', '/api/autodispatch/snapshot')
            self.assertGreater({t['id']: t for t in snap['tasks']}['IP-01']['recovery']['manualPriority'], 0)
            queue.claim('IP-01')
            status, _ = self.post_json('/api/autodispatch/tasks/IP-01/retry', {})
            self.assertEqual(409, status)
        finally:
            store.close()

    def test_legacy_job_preview_uses_original_arrival_without_writing(self):
        store = Store(self.root)
        try:
            queue = RecoveryQueue(store)
            queue.enqueue('IP-01', {'path': 'snapshot', 'sha256': 'hash'}, 'failure', now=100)
            rec = store.records()['IP-01']
            rec['recovery'].pop('enqueuedAt', None)
            with store.db:
                store.db.execute('UPDATE tasks SET payload=? WHERE id=?', (json.dumps(rec), 'IP-01'))
            before = store.history('IP-01')
            _, snap = self.request('GET', '/api/autodispatch/snapshot')
            recovery = {t['id']: t for t in snap['tasks']}['IP-01']['recovery']
            self.assertEqual(100000, recovery['enqueuedAt'])
            self.assertEqual(before, store.history('IP-01'))
        finally:
            store.close()

    def test_rejects_foreign_host_and_non_json_bodies(self):
        status, _ = self.request("GET", "/api/autodispatch/snapshot", headers={"Host": "evil.example:8765"})
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/api/autodispatch/control", "{}",
                                 {"Host": f"127.0.0.1:{self.port}", "Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _ = self.request("GET", "/api/autodispatch/unknown")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()

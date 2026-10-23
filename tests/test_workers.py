import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autodispatch.engine import Engine
from autodispatch.model import Blocked, load_config
from autodispatch.safety import approval_fingerprint
from autodispatch.store import Store
from autodispatch.workers import (dag_width, default_profile, worker_capacity, execution_config, reserve_worker,
                                 save_profile, selected_profile, settings_snapshot)


def plan_for(dependencies):
    return SimpleNamespace(tasks={tid: SimpleNamespace(id=tid, dependencies=deps, agent='Codex',
        scope_write=(f'src/{tid}.py',)) for tid, deps in dependencies.items()})


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.c = {'max_workers': 'auto', 'codex_model': 'model-a', 'worker_model_selection': 'official'}
        catalog_patch = patch('autodispatch.model_catalog.catalog', return_value={
            'status': 'ready', 'source': 'Official CLI fixture', 'models': [
                {'model': 'model-a', 'reasoningEfforts': [None, 'low', 'high']},
                {'model': 'model-b', 'reasoningEfforts': [None, 'medium']}]})
        self.catalog_mock = catalog_patch.start()
        self.addCleanup(catalog_patch.stop)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_width_is_maximum_antichain_not_largest_topological_layer(self):
        plan = plan_for({'a': (), 'b': ('a',), 'c': ('b',), 'd': ('a',), 'e': ('b',)})
        self.assertEqual(3, dag_width(plan.tasks))
        self.assertEqual(1, dag_width(plan.tasks, ['a', 'c']))
        self.assertEqual(0, dag_width({}))
        self.assertEqual(3, worker_capacity(self.c, plan))
        self.assertEqual(2, worker_capacity(dict(self.c, max_workers=2), plan))

    def test_auto_engine_runs_three_independent_tasks_and_retires_slots(self):
        plan = plan_for({'a': (), 'b': (), 'c': (), 'd': ('a', 'b', 'c')})
        backend = SimpleNamespace(c=self.c, current_base=lambda: 'base',
            dependency_valid=lambda record, base: record.get('state') == 'MERGED_VERIFIED')
        barrier = threading.Barrier(3)
        observed = []
        store = self.store
        class RecordingEngine(Engine):
            def task(self, task, base):
                if task.id != 'd':
                    barrier.wait(timeout=5)
                observed.append((task.id, store.records()[task.id]['worker_slot']))
                store.update(task.id, 'MERGED_VERIFIED')
        result = RecordingEngine(plan, store, backend, list(plan.tasks)).run(max_workers='auto', max_tasks=4)
        self.assertEqual(4, result['launched_tasks'])
        self.assertEqual({1, 2, 3}, {slot for tid, slot in observed if tid != 'd'})
        self.assertEqual('d', observed[-1][0])
        self.assertIn(observed[-1][1], {1, 2, 3})
        self.assertTrue(all(not r['worker_active'] for r in store.records().values()))

    def test_saved_selection_is_bound_to_config_and_only_affects_next_task(self):
        task = plan_for({'a': ()}).tasks['a']
        first = {'model': 'model-a', 'reasoningEffort': 'high'}
        second = {'model': 'model-b', 'reasoningEffort': 'medium'}
        save_profile(self.store, self.c, 1, 'Codex', first)
        reserve_worker(self.store, self.c, task, 1, 'base')
        save_profile(self.store, self.c, 1, 'Codex', second)
        configured = execution_config(self.c, self.store.records()['a'], 'Codex')
        self.assertEqual('model-a', configured['codex_model'])
        self.assertEqual('high', configured['codex_reasoning_effort'])
        saved = settings_snapshot(self.store.root)
        self.assertEqual(second, selected_profile(self.c, saved, 1, 'Codex'))
        self.assertEqual({'model': 'model-a', 'reasoningEffort': None},
                         selected_profile(dict(self.c, max_workers=1), saved, 1, 'Codex'))
        self.assertNotIn('codex_reasoning_effort', self.c)
        self.assertEqual(0, self.store.agent_requests_used())

    def test_unapproved_model_effort_combinations_are_rejected(self):
        for profile in ({'model': 'model-c', 'reasoningEffort': None},
                        {'model': 'model-b', 'reasoningEffort': 'high'}):
            with self.assertRaises(Blocked):
                save_profile(self.store, self.c, 1, 'Codex', profile)
        self.assertEqual({}, settings_snapshot(self.store.root))

    def test_new_settings_change_fingerprint_but_legacy_fingerprint_is_preserved(self):
        base = {'max_workers': 2}
        self.assertNotEqual(approval_fingerprint(base), approval_fingerprint(dict(base, max_workers='auto')))
        self.assertNotEqual(approval_fingerprint(base), approval_fingerprint(dict(base, codex_reasoning_effort='high')))
        self.assertNotEqual(approval_fingerprint(base), approval_fingerprint(dict(base, worker_model_selection='official')))
        self.assertEqual('fd3714a65aebe61eefbbe1c82ff029145b6498bb31fcb11a0a59d3afddd0e79e', approval_fingerprint(base))

    def test_existing_process_or_ambiguous_launch_prevents_worker_reuse(self):
        plan = plan_for({'a': ()})
        backend = SimpleNamespace(c=self.c)
        for fields in ({'pid': os.getpid()}, {'pid': None, 'pending_launch': True}):
            self.store.update('a', 'BLOCKED', **fields)
            result = Engine(plan, self.store, backend, ['a']).run()
            self.assertEqual(0, result['launched_tasks'])
            self.assertIn('process still owns', result['blocked_reason'])

    def test_opencode_inline_variant_remains_compatible(self):
        self.assertEqual({'model': 'provider/model', 'reasoningEffort': 'high'},
                         default_profile({'opencode_model': 'provider/model#high'}, 'OpenCode'))

    def test_config_accepts_auto_and_larger_finite_legacy_limits(self):
        sample = json.loads((Path(__file__).resolve().parents[1] / 'config.example.json').read_text())
        path = self.store.root / 'config.json'
        for workers in ('auto', 4):
            path.write_text(json.dumps(dict(sample, max_workers=workers)))
            self.assertEqual(workers, load_config(path)['max_workers'])

    def test_scope_conflicts_and_numeric_cap_still_limit_actual_workers(self):
        for scope_conflict, limit, expected in ((True, 'auto', 1), (False, 2, 2)):
            with self.subTest(scope_conflict=scope_conflict):
                plan = plan_for({'a': (), 'b': (), 'c': ()})
                if scope_conflict:
                    for task in plan.tasks.values():
                        task.scope_write = ('src/shared.py',)
                backend = SimpleNamespace(c=self.c, current_base=lambda: 'base', dependency_valid=lambda *_: True)
                count, peak = [0], [0]
                lock = threading.Lock()
                store = self.store
                for tid in plan.tasks:
                    store.update(tid, 'PENDING', worktree=None)
                class RecordingEngine(Engine):
                    def task(self, task, base):
                        with lock:
                            count[0] += 1
                            peak[0] = max(peak[0], count[0])
                        time.sleep(0.04)
                        store.update(task.id, 'MERGED_VERIFIED')
                        with lock:
                            count[0] -= 1
                RecordingEngine(plan, store, backend, list(plan.tasks)).run(max_workers=limit, max_tasks=3)
                self.assertEqual(expected, peak[0])

    def test_stop_and_budget_are_rechecked_between_candidate_reservations(self):
        plan = plan_for({'a': (), 'b': (), 'c': ()})
        backend = SimpleNamespace(c=self.c, current_base=lambda: 'base', dependency_valid=lambda *_: True,
                                  runner=SimpleNamespace(max_requests=1))
        store = self.store
        class RecordingEngine(Engine):
            def task(self, task, base):
                store.update(task.id, 'MERGED_VERIFIED')
        original = reserve_worker
        def reserve_then_stop(*args):
            result = original(*args)
            (store.root / 'stop.request').write_text('stop')
            return result
        with patch('autodispatch.engine.reserve_worker', side_effect=reserve_then_stop):
            result = RecordingEngine(plan, store, backend, list(plan.tasks)).run(max_tasks=3)
        self.assertEqual(1, result['launched_tasks'])
        (store.root / 'stop.request').unlink()
        def reserve_then_charge(*args):
            result = original(*args)
            store.charge(1)
            return result
        with patch('autodispatch.engine.reserve_worker', side_effect=reserve_then_charge):
            result = RecordingEngine(plan, store, backend, list(plan.tasks)).run(max_tasks=3)
        self.assertEqual(1, result['launched_tasks'])
        self.assertTrue(result['budget_exhausted'])

    def test_catalog_validation_does_not_hold_database_writer_lock(self):
        task = plan_for({'a': ()}).tasks['a']
        second = Store(self.store.root)
        try:
            with patch('autodispatch.workers.validate_profile', side_effect=lambda *_: second.charge(10)):
                reserve_worker(self.store, self.c, task, 1, 'base')
            self.assertEqual(1, second.agent_requests_used())
        finally:
            second.close()

    def test_running_profile_survives_catalog_outage(self):
        task = plan_for({'a': ()}).tasks['a']
        save_profile(self.store, self.c, 1, 'Codex', {'model': 'model-b', 'reasoningEffort': 'medium'})
        reserve_worker(self.store, self.c, task, 1, 'base')
        self.catalog_mock.side_effect = AssertionError('Running stages must not requery the catalog')
        config = execution_config(self.c, self.store.records()['a'], 'Codex')
        self.assertEqual('model-b', config['codex_model'])
        self.assertEqual('medium', config['codex_reasoning_effort'])

    def test_unavailable_worker_preference_does_not_block_other_slots(self):
        save_profile(self.store, self.c, 1, 'Codex', {'model': 'model-b', 'reasoningEffort': 'medium'})
        self.catalog_mock.return_value = {'status': 'ready', 'models': []}
        plan = plan_for({'a': (), 'b': ()})
        backend = SimpleNamespace(c=self.c, current_base=lambda: 'base', dependency_valid=lambda *_: True)
        store = self.store
        slots = []
        class RecordingEngine(Engine):
            def task(self, task, base):
                slots.append(store.records()[task.id]['worker_slot'])
                store.update(task.id, 'MERGED_VERIFIED')
        result = RecordingEngine(plan, store, backend, list(plan.tasks)).run(max_tasks=2)
        self.assertEqual(2, result['launched_tasks'])
        self.assertEqual([2, 2], slots)
        self.assertIn('worker:issue:1:Codex', settings_snapshot(store.root))
        save_profile(store, self.c, 1, 'Codex', {'model': 'model-a', 'reasoningEffort': None})
        self.assertNotIn('worker:issue:1:Codex', settings_snapshot(store.root))

    def test_correcting_worker_preference_reenables_slot_during_same_run(self):
        self.c['max_workers'] = 2
        save_profile(self.store, self.c, 1, 'Codex', {'model': 'model-b', 'reasoningEffort': 'medium'})
        self.catalog_mock.return_value = {'status': 'ready', 'models': []}
        plan = plan_for({'a': (), 'b': (), 'c': ()})
        backend = SimpleNamespace(c=self.c, current_base=lambda: 'base', dependency_valid=lambda *_: True)
        store, config = self.store, self.c
        slots = []
        class RecordingEngine(Engine):
            def task(self, task, base):
                slots.append(store.records()[task.id]['worker_slot'])
                if task.id == 'a':
                    save_profile(store, config, 1, 'Codex', {'model': 'model-a', 'reasoningEffort': None})
                store.update(task.id, 'MERGED_VERIFIED')
        result = RecordingEngine(plan, store, backend, list(plan.tasks)).run(max_tasks=3)
        self.assertEqual(3, result['launched_tasks'])
        self.assertEqual(2, slots[0])
        self.assertIn(1, slots[1:])
        self.assertEqual([], result['worker_configuration_blocked'])

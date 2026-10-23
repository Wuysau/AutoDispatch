"""DAG-bounded capacity and durable, configuration-bound worker preferences."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

from .model import Blocked
from .safety import approval_fingerprint

EXECUTORS = {'Codex': ('codex_model', 'codex_reasoning_effort'),
             'OpenCode': ('opencode_model', 'opencode_variant')}


def dag_width(tasks, selected=None):
    """Maximum antichain via transitive closure and bipartite matching (Dilworth)."""
    selected = set(tasks) if selected is None else set(selected) & set(tasks)
    ancestors = {}
    visiting = set()

    def visit(tid):
        if tid in visiting or tid not in tasks:
            raise Blocked('DAG cycle or unknown dependency')
        if tid not in ancestors:
            visiting.add(tid)
            reachable = set()
            for dep in tasks[tid].dependencies:
                reachable.add(dep)
                reachable.update(visit(dep))
            visiting.remove(tid)
            ancestors[tid] = reachable
        return ancestors[tid]

    for tid in tasks:
        visit(tid)
    matched = {}

    def augment(tid, seen):
        for dep in sorted(ancestors[tid] & selected):
            if dep in seen:
                continue
            seen.add(dep)
            if dep not in matched or augment(matched[dep], seen):
                matched[dep] = tid
                return True
        return False

    return len(selected) - sum(augment(tid, set()) for tid in sorted(selected))


def worker_capacity(c, plan, authorization=None):
    selected = (set(plan.tasks) if authorization is None else set(authorization)) - {'IP-00', 'IP-34'}
    width = dag_width(plan.tasks, selected)
    limit = c.get('max_workers', 'auto')
    if limit != 'auto' and (type(limit) is not int or limit < 1):
        raise Blocked('max_workers must be auto or a positive integer')
    return width if limit == 'auto' else min(width, limit)


def default_profile(c, executor):
    model_key, effort_key = EXECUTORS[executor]
    model, effort = c.get(model_key), c.get(effort_key)
    if executor == 'OpenCode' and model and '#' in model:
        model, inline = model.rsplit('#', 1)
        effort = effort or inline
    return {'model': model, 'reasoningEffort': effort}


def model_options(c, executor, wait=False):
    from .model_catalog import catalog
    default = default_profile(c, executor)
    if c.get('worker_model_selection') != 'official':
        return []
    result = catalog(c, executor, wait=wait)
    if result['status'] != 'ready':
        return []
    defaults = [{'model': None, 'reasoningEfforts': [None]}] if default['model'] is None else []
    return [*defaults, *result['models']]


def validate_worker_config(c):
    def identifier(value):
        return value is None or (isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}', value))
    for model_key, effort_key in EXECUTORS.values():
        profile = default_profile(c, 'Codex' if model_key == 'codex_model' else 'OpenCode')
        if not identifier(profile['model']) or not identifier(profile['reasoningEffort']):
            raise Blocked('Invalid worker model or reasoning effort')
    if c.get('opencode_variant') and not c.get('opencode_model'):
        raise Blocked('OpenCode variant requires an explicit provider/model')
    if c.get('worker_model_selection') not in {None, 'official'}:
        raise Blocked('Worker model selections must come from official catalogs')


def validate_profile(c, executor, profile):
    if not isinstance(executor, str) or executor not in EXECUTORS or not isinstance(profile, dict) or set(profile) != {'model', 'reasoningEffort'}:
        raise Blocked('无效的 Worker 配置')
    if profile == default_profile(c, executor):
        return
    if not any(profile['model'] == option['model'] and profile['reasoningEffort'] in option['reasoningEfforts']
               for option in model_options(c, executor, wait=True)):
        raise Blocked('当前官方目录未提供该模型或思考强度，或此配置尚未启用模型选择')


def settings_snapshot(root: Path):
    path = root / 'state.sqlite3'
    if not path.is_file():
        return {}
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        return {key: json.loads(raw) for key, raw in db.execute(
            "SELECT key,value FROM meta WHERE key LIKE 'worker:%' OR key='scheduler'")}
    finally:
        db.close()


def selected_profile(c, settings, slot, executor):
    saved = settings.get(f'worker:{slot}:{executor}', {})
    if saved.get('configuration_sha256') != approval_fingerprint(c):
        return default_profile(c, executor)
    return saved['profile']


def save_profile(store, c, slot, executor, profile):
    validate_profile(c, executor, profile)
    payload = {'profile': profile, 'configuration_sha256': approval_fingerprint(c), 'updated_at': time.time()}
    with store.lock, store.db:
        store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                         (f'worker:{slot}:{executor}', json.dumps(payload)))
        store.db.execute('DELETE FROM meta WHERE key=?', (f'worker:issue:{slot}:{executor}',))
    return payload


def record_worker_issue(store, c, slot, executor, reason):
    payload = {'slot': slot, 'executor': executor, 'issue': str(reason),
               'configuration_sha256': approval_fingerprint(c)}
    with store.lock, store.db:
        store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                         (f'worker:issue:{slot}:{executor}', json.dumps(payload)))


def reserve_worker(store, c, task, slot, base):
    """Pin preferences and task ownership in one transaction before worker launch."""
    key = f'worker:{slot}:{task.agent}'
    def read_profile():
        row = store.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        settings = {key: json.loads(row[0])} if row else {}
        return selected_profile(c, settings, slot, task.agent)
    for _ in range(3):
        with store.lock:
            profile = read_profile()
        # A remote metadata refresh must never hold SQLite's writer lock.
        validate_profile(c, task.agent, profile)
        with store.lock, store.db:
            store.db.execute('BEGIN IMMEDIATE')
            if read_profile() != profile:
                continue
            store.db.execute('DELETE FROM meta WHERE key=?', (f'worker:issue:{slot}:{task.agent}',))
            current = store.records().get(task.id, {})
            return store.update(task.id, 'RUNNING', worktree=current.get('worktree', f'reserved:{task.id}'),
                                base_sha=current.get('base_sha', base), worker_slot=slot, executor=task.agent,
                                worker_active=True, coordinator_pid=os.getpid(),
                                worker_started_at=time.time(), worker_profile=profile,
                                worker_configuration_sha256=approval_fingerprint(c))
    raise Blocked('Worker 配置持续变化，请待配置保存完成后重试')


def execution_config(c, record, executor):
    """Never mutate the shared backend config or a running task's pinned profile."""
    profile = record.get('worker_profile')
    if not profile or record.get('executor') != executor:
        return c
    if record.get('worker_configuration_sha256') != approval_fingerprint(c):
        raise Blocked('Worker selection belongs to another configuration')
    # The profile was validated against official capabilities at reservation.
    # Subsequent stages use that immutable selection even during catalog outages.
    if (not isinstance(profile, dict) or set(profile) != {'model', 'reasoningEffort'}
            or any(value is not None and not isinstance(value, str) for value in profile.values())):
        raise Blocked('Invalid pinned Worker profile')
    if profile != default_profile(c, executor) and c.get('worker_model_selection') != 'official':
        raise Blocked('Worker model selection is not enabled for this configuration')
    model_key, effort_key = EXECUTORS[executor]
    return dict(c, **{model_key: profile['model'], effort_key: profile['reasoningEffort']})

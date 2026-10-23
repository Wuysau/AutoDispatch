"""Scoped source snapshots, not copies of host environments or credentials."""
from __future__ import annotations

import json
import platform
import shutil
import sys
import uuid
from pathlib import Path

from .control import atomic_json
from .model import Blocked, digest, validate_spec, within
from .safety import check_scope, redact
from .verification import receipt_valid


def classify_failure(record):
    reason = record.get('reason', '')
    if record.get('local_merge_intent'):
        return 'AMBIGUOUS_INTEGRATION', False
    if record.get('pending_launch'):
        return 'AMBIGUOUS_PROCESS', False
    protected = ('Scope expansion', 'SPEC', 'Manifest', 'manifest', 'Potential secret',
                 'Remote identity', 'Agent changed HEAD', 'Agent changed branch', 'qualification gate',
                 'sandbox rejection', 'command is rejected', 'permission denied')
    if any(x in reason for x in protected):
        return 'PROTECTED_BOUNDARY', False
    receipt = record.get('last_receipt', {})
    if receipt.get('stopped'):
        return 'USER_STOP', False
    if receipt.get('timeout') or receipt.get('exit_code') == 124:
        return 'TIMEOUT', True
    if 'trailing whitespace' in reason:
        return 'FORMAT', True
    return 'EXECUTION', True


def completed_message(record, state_root):
    receipt = record.get('last_receipt', {})
    if receipt.get('exit_code') != 0 or not receipt_valid(receipt):
        return False
    argv = receipt.get('argv', [])
    if '--output-last-message' in argv:
        path = Path(argv[argv.index('--output-last-message') + 1]).resolve()
        if not path.is_relative_to((state_root / 'outputs').resolve()):
            return False
        return path.is_file() and path.read_text(encoding='utf-8').lstrip().startswith('CODE_READY_VERIFY_PENDING')
    messages = []
    for line in Path(receipt['log']).read_text(encoding='utf-8').splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('type') == 'text':
            messages.append(event.get('part', {}).get('text', ''))
    return bool(messages and messages[-1].lstrip().startswith('CODE_READY_VERIFY_PENDING'))


def capture(backend, task, record):
    path = Path(record['worktree']).resolve()
    if not path.is_relative_to(Path(backend.c['worktree_root']).resolve()):
        raise Blocked('Failure worktree is outside the approved root')
    validate_spec(path, backend.c['manifest_sha256'])
    if backend.git.text(path, 'ls-files', '-u'):
        raise Blocked('Unmerged index needs explicit conflict inspection')
    branch = backend.git.text(path, 'branch', '--show-current')
    if branch != record.get('branch'):
        raise Blocked('Failed task branch changed')
    sha = backend.git.sha(path)
    tracked = backend.git.text(path, 'diff', '--name-only', 'HEAD').splitlines()
    untracked = backend.git.text(path, 'ls-files', '--others', '--exclude-standard').splitlines()
    paths = sorted(set(tracked + untracked))
    if paths:
        check_scope(task, paths, path)
    # Check committed changes as well; snapshot data never grants wider ownership.
    committed = backend.git.text(path, 'diff', '--name-only', record['base_sha'] + '...HEAD').splitlines()
    if committed:
        check_scope(task, committed, path)
    folder = backend.root / 'recovery-snapshots' / task.id / uuid.uuid4().hex
    folder.mkdir(parents=True)
    patch = folder / 'tracked.patch'
    # Git writes raw bytes directly; redacting stdout must never corrupt a source patch.
    backend.git.text(path, 'diff', '--binary', f'--output={patch}', 'HEAD', '--', *tracked) if tracked else patch.write_bytes(b'')
    files = {}
    for name in untracked:
        source = within(path, name)
        if source.is_symlink():
            raise Blocked('Snapshot cannot include a symlink')
        target = within(folder / 'files', name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        files[name] = digest(target.read_bytes())
    kind = 'agent'
    if not record.get('force_agent_repair') and completed_message(record, backend.root):
        kind = 'commit' if paths else 'integrate'
    receipt = record.get('last_receipt', {})
    metadata = {
        'task_id': task.id, 'head_sha': sha, 'base_sha': record['base_sha'], 'branch': branch,
        'worktree': str(path), 'kind': kind, 'patch_sha256': digest(patch.read_bytes()),
        'tracked_paths': tracked, 'files': files, 'failure': redact(record.get('reason', ''))[-6000:],
        'receipt': receipt, 'record': record,
        'environment': {'os': platform.system(), 'release': platform.release(), 'arch': platform.machine(),
                        'python': sys.version.split()[0], 'executor': task.agent,
                        'requested_model': record.get('requested_model'), 'verify_command': task.verify_command,
                        'manifest_sha256': backend.c['manifest_sha256'],
                        'note': 'No host credentials, environment secrets, .venv or node_modules copied'},
    }
    manifest = folder / 'snapshot.json'
    atomic_json(manifest, metadata)
    return {'path': str(manifest), 'sha256': digest(manifest.read_bytes())}


def restore(backend, task, snapshot, attempt, base):
    manifest = Path(snapshot['path']).resolve()
    if not manifest.is_relative_to((backend.root / 'recovery-snapshots').resolve()):
        raise Blocked('Snapshot path escaped the state directory')
    if digest(manifest.read_bytes()) != snapshot['sha256']:
        raise Blocked('Snapshot manifest hash mismatch')
    data = json.loads(manifest.read_text(encoding='utf-8'))
    if data['task_id'] != task.id or data['environment']['manifest_sha256'] != backend.c['manifest_sha256']:
        raise Blocked('Snapshot task/spec identity mismatch')
    folder = manifest.parent
    patch = folder / 'tracked.patch'
    if digest(patch.read_bytes()) != data['patch_sha256']:
        raise Blocked('Snapshot patch hash mismatch')
    paths = sorted(set(data['tracked_paths'] + list(data['files'])))
    if paths:
        # Check archived code before it can be restored, including credential exclusion.
        from .safety import allowed
        if any(not allowed(p, task.scope_write) or p.startswith('docs/spec/') for p in paths):
            raise Blocked('Snapshot contains a path outside task ownership')
    root = Path(backend.c['worktree_root'])
    suffix = uuid.uuid4().hex[:8]
    target = root / f'{task.id.lower()}-recovery-{attempt}-{suffix}'
    branch = f'codex/autodispatch/{task.id.lower()}-recovery-{attempt}-{suffix}'
    backend.git.create(backend.repo, target, data['head_sha'], branch)
    backend.git.text(target, 'merge', '--no-edit', base)
    if patch.stat().st_size:
        backend.git.text(target, 'apply', '--binary', '--whitespace=nowarn', str(patch))
    for name, expected in data['files'].items():
        source = within(folder / 'files', name)
        if digest(source.read_bytes()) != expected:
            raise Blocked('Archived source hash mismatch: ' + name)
        destination = within(target, name)
        if destination.exists() and digest(destination.read_bytes()) != expected:
            raise Blocked('Restoration would overwrite changed code: ' + name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    if paths:
        check_scope(task, paths, target)
    validate_spec(target, backend.c['manifest_sha256'])
    return {'worktree': str(target), 'branch': branch, 'base_sha': base}, data

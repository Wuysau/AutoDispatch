"""Read model capabilities from official CLI discovery APIs; never start a turn."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from .model import Blocked
from .process import clean_env, terminate_tree
from .process_tree import ProcessTree

SOURCES = {'Codex': 'Codex model/list', 'OpenCode': 'OpenCode model.list'}
_cache = {}
_lock = threading.Lock()
TTL = 900


def codex_models(rows):
    return [{'model': row['model'], 'displayName': row.get('displayName', row['model']),
             'defaultReasoningEffort': row.get('defaultReasoningEffort'),
             'reasoningEfforts': [None, *[effort['reasoningEffort'] for effort in row.get('supportedReasoningEfforts', [])
                                         if isinstance(effort, dict) and isinstance(effort.get('reasoningEffort'), str)]]}
            for row in rows if isinstance(row, dict) and not row.get('hidden') and isinstance(row.get('model'), str)]


def opencode_models(rows):
    # Do not retain settings, headers, credentials, prompts, or raw responses.
    return [{'model': row['providerID'] + '/' + row['id'], 'displayName': row.get('name', row['id']),
             'reasoningEfforts': [None, *[variant['id'] for variant in row.get('variants', [])
                                        if isinstance(variant, dict) and isinstance(variant.get('id'), str) and variant.get('enabled') is not False]]}
            for row in rows if isinstance(row, dict) and row.get('enabled') is not False
            and isinstance(row.get('providerID'), str) and isinstance(row.get('id'), str)]


def _command(c, executor):
    command = c.get('codex_command' if executor == 'Codex' else 'opencode_command')
    if not command or Path(command[0]).suffix.lower() in {'.cmd', '.bat', '.ps1'}:
        raise Blocked('未配置官方 CLI 可执行文件')
    if executor == 'OpenCode' and c.get('opencode_environment') == 'wsl':
        from .safety import wsl_argv
        w = c['wsl']
        return wsl_argv(w['distribution'], w['repo'], [*w['opencode_command'], 'api', 'model.list', '--standalone'])
    return [*command, *(['app-server'] if executor == 'Codex' else ['api', 'model.list', '--standalone'])]


def discover(c, executor):
    argv = _command(c, executor)
    if os.name == 'nt':
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        kwargs = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NEW_CONSOLE,
                  'startupinfo': startup}
    else:
        kwargs = {'start_new_session': True}
    launched = [sys.executable, str(Path(__file__).with_name('spawn_gate.py')), json.dumps(argv)] if os.name == 'nt' else argv
    p = subprocess.Popen(launched, cwd=c.get('repo') or None, env=clean_env(), stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8', **kwargs)
    tree = None
    reader = None
    try:
        tree = ProcessTree(p)
        if os.name == 'nt':
            p.stdin.write('G')
            p.stdin.flush()
        if executor == 'OpenCode':
            stdout, _ = p.communicate(timeout=30)
            if p.returncode:
                raise Blocked('OpenCode 官方模型目录读取失败')
            result = json.loads(stdout)
            location = result.get('location', {}).get('directory')
            if c.get('opencode_environment') == 'wsl':
                matches = location == c['wsl']['repo']
            else:
                matches = location and Path(location).resolve() == Path(c.get('repo') or Path.cwd()).resolve()
            if not matches:
                raise Blocked('OpenCode 返回了不同项目的模型目录')
            return opencode_models(result['data'])
        inbox = queue.Queue()
        def read():
            try:
                for line in p.stdout:
                    message = json.loads(line)
                    if isinstance(message, dict):
                        inbox.put(message)
            except (ValueError, OSError):
                pass
            finally:
                inbox.put(None)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        deadline = time.monotonic() + 25
        sequence = 0
        def send(message):
            p.stdin.write(json.dumps(message) + '\n')
            p.stdin.flush()
        def request(method, params):
            nonlocal sequence
            sequence += 1
            send({'id': sequence, 'method': method, 'params': params})
            while True:
                if time.monotonic() >= deadline:
                    raise Blocked('官方模型目录请求超时')
                message = inbox.get(timeout=max(0.001, deadline - time.monotonic()))
                if message is None:
                    raise Blocked('Codex 官方模型目录连接已关闭')
                if message.get('id') == sequence:
                    if 'error' in message:
                        raise Blocked('Codex 官方模型目录请求失败')
                    return message['result']
        request('initialize', {'clientInfo': {'name': 'autodispatch_catalog', 'title': 'AutoDispatch model catalog', 'version': '0.1'}})
        send({'method': 'initialized', 'params': {}})
        rows, cursor = [], None
        while True:
            result = request('model/list', {'limit': 100, 'includeHidden': False, 'cursor': cursor})
            rows.extend(result['data'])
            cursor = result.get('nextCursor')
            if not cursor:
                return codex_models(rows)
    finally:
        if tree:
            tree.close()
        elif p.poll() is None:
            terminate_tree(p)
        if p.poll() is None:
            p.kill()
        p.wait(timeout=5)
        if p.stdin and not p.stdin.closed:
            p.stdin.close()
        if reader:
            reader.join(timeout=2)
        if p.stdout and not p.stdout.closed:
            p.stdout.close()


def _key(c, executor):
    return (executor, str(c.get('repo')), tuple(c.get('codex_command' if executor == 'Codex' else 'opencode_command', [])),
            json.dumps(c.get('wsl', {}) if executor == 'OpenCode' and c.get('opencode_environment') == 'wsl' else {}, sort_keys=True))


def _fetch(c, executor, key, event):
    try:
        models = discover(c, executor)
        if not models:
            raise Blocked('官方目录没有返回可用模型')
        result = {'status': 'ready', 'source': SOURCES[executor], 'models': models,
                  'updatedAt': int(time.time() * 1000)}
    except Exception:
        # A changed upstream schema must fail closed, never leave selectors loading forever.
        result = {'status': 'unavailable', 'source': SOURCES[executor], 'models': [],
                  'error': '官方模型目录暂不可用，请检查执行器连接后重试', 'updatedAt': int(time.time() * 1000)}
    with _lock:
        _cache[key] = (time.monotonic(), result, event)
        event.set()


def catalog(c, executor, wait=False, refresh=False):
    key = _key(c, executor)
    with _lock:
        cached = _cache.get(key)
        if cached and cached[1]['status'] == 'loading':
            event = cached[2]
        elif not refresh and cached and time.monotonic() - cached[0] < (30 if cached[1]['status'] == 'unavailable' else TTL):
            return cached[1]
        else:
            event = threading.Event()
            _cache[key] = (time.monotonic(), {'status': 'loading', 'source': SOURCES[executor], 'models': []}, event)
            threading.Thread(target=_fetch, args=(dict(c), executor, key, event), daemon=True).start()
        result = _cache[key][1]
    if wait:
        event.wait(timeout=35)
        with _lock:
            result = _cache[key][1]
    return result

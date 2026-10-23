"""Opt-in automatic diagnosis and recovery on idle batch workers."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

from .batch import BatchBackend, BatchEngine, INTEGRATED, batch_approval, final_checks
from .context import context
from .control import atomic_json
from .handoff import service_lease
from .model import Blocked, Plan, digest, load_config, validate_spec
from .recovery_queue import RecoveryQueue
from .workers import worker_capacity
from .recovery_snapshot import capture, classify_failure, restore
from .safety import approval_fingerprint, check_scope, conflicts, require_approval
from .store import CoordinatorLock, Store, process_alive


def recovery_approval(c, path, batch):
    policy = json.loads(path.read_text(encoding='utf-8'))
    if (policy.get('configuration_sha256') != approval_fingerprint(c)
            or policy.get('batch_id') != batch['batch_id'] or policy.get('enabled') is not True
            or policy.get('max_attempts') != 2 or not policy.get('approved_at')
            or not policy.get('user_request')):
        raise Blocked('Explicit recovery authorization must bind this batch, configuration and two-attempt limit')
    return policy


class RecoveryBackend(BatchBackend):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.claims = {}

    def prepare(self, task, base):
        claim = self.claims.get(task.id)
        if not claim:
            return super().prepare(task, base)
        work, data = restore(self, task, claim['snapshot'], claim['attempts'], base)
        claim['restored'] = data
        return work

    def remaining(self, task):
        claim = self.claims.get(task.id)
        if not claim:
            return super().remaining(task)
        remaining = int(claim['attempt_deadline'] - time.time())
        if remaining < 1:
            raise Blocked('Recovery active-time budget exhausted')
        return remaining

    def agent_context(self, task, stage, sha, evidence, path):
        claim = self.claims.get(task.id)
        if not claim:
            return super().agent_context(task, stage, sha, evidence, path)
        records = self.store.records()
        data = claim['restored']
        receipt = data.get('receipt', {})
        log_excerpt = ''
        log_path = Path(receipt.get('log', ''))
        if log_path.is_file() and log_path.resolve().is_relative_to((self.root / 'logs').resolve()):
            from .safety import redact
            log_excerpt = redact(log_path.read_text(encoding='utf-8', errors='replace')[-14000:])
        return context(self.plan, task, stage, sha, {
            'user_workflow': self.request['user_request'], 'recovery_attempt': claim['attempts'],
            'maximum_recovery_attempts': claim['max_attempts'], 'error_class': claim['error_class'],
            'failure': claim['error'], 'failure_log_excerpt': log_excerpt,
            'restored_from': {'head_sha': data['head_sha'], 'tracked_paths': data['tracked_paths'],
                              'untracked_paths': list(data['files']), 'environment': data['environment']},
            'attempt_timeout_seconds': max(0, int(claim['attempt_deadline'] - time.time())),
            'dependencies': {dep: {k: records[dep].get(k) for k in ('state', 'commit_sha', 'merge_sha')}
                             for dep in task.dependencies},
            'instructions': 'Diagnose the recorded cause; continue the restored partial implementation. '
                            'Do not treat deferred tests or unverified dependency code as blockers to coding. '
                            'Do not remove funds-safety gates or claim native qualification passed.',
        }, repo=path)

    def run_agent(self, task, stage, work, evidence=None):
        claim = self.claims.get(task.id)
        if not claim or claim['restored']['kind'] == 'agent':
            return super().run_agent(task, stage, work, evidence)
        path = Path(work['worktree'])
        changed = set(self.git.text(path, 'diff', '--name-only', 'HEAD').splitlines())
        changed |= set(self.git.text(path, 'ls-files', '--others', '--exclude-standard').splitlines())
        if changed:
            check_scope(task, sorted(changed), path)
            self.git.text(path, 'diff', '--check', 'HEAD')
            self.git.text(path, 'add', '--', *sorted(changed))
            self.git.text(path, 'commit', '-m', f'feat({task.id.lower()}): resume completed recovery handoff')
        sha = self.git.sha(path)
        self.audit(task, work, sha)
        return sha


class RecoveryEngine(BatchEngine):
    def __init__(self, *args, queue=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.queue = queue or RecoveryQueue(self.store)

    def implementation_stage(self, task):
        return 'batch-repair' if task.id in self.backend.claims else 'batch-implement'

    def retry_count(self, task):
        return self.backend.claims.get(task.id, {}).get('attempts', 0)

    def record_failure(self, task, claimed=None):
        record = self.store.records()[task.id]
        error_class, retryable = classify_failure(record)
        if process_alive(record.get('pid')) or record.get('pending_launch'):
            error_class, retryable = 'AMBIGUOUS_PROCESS', False
        receipt = record.get('last_receipt', {})
        try:
            if not retryable:
                raise Blocked(error_class)
            snapshot = capture(self.backend, task, record)
        except (Blocked, OSError, ValueError) as exc:
            snapshot, retryable = None, False
            error_class = error_class if error_class != 'EXECUTION' else 'SNAPSHOT_BLOCKED'
            record['reason'] += '\nSnapshot: ' + str(exc)
        self.queue.enqueue(task.id, snapshot, record['reason'], error_class, retryable,
                           elapsed=receipt.get('elapsed_seconds', 0),
                           receipt_key=receipt.get('receipt_path', ''), claimed=claimed)

    def ready(self, base):
        # New eligible work goes first; recovery uses an otherwise idle worker.
        ready = super().ready(base)
        records = self.store.records()
        for job in self.queue.ready():
            tid = job['task_id']
            if tid not in self.authorization or records.get(tid, {}).get('state') != 'RECOVERY_QUEUED':
                continue
            task = self.plan.tasks[tid]
            if not all(self.backend.dependency_valid(records.get(d, {}), base) for d in task.dependencies):
                continue
            holders = [self.plan.tasks[other] for other, rec in records.items()
                       if other != tid and other in self.plan.tasks and rec.get('worktree')
                       and rec['state'] not in {INTEGRATED, 'MERGED_VERIFIED'}]
            if not any(conflicts(task, held) for held in holders + ready):
                ready.append(task)
        return ready

    def task(self, task, base):
        job = self.queue.records().get(task.id)
        claim = None
        if job and job['state'] == 'QUEUED':
            claim = self.queue.claim(task.id)
            if not claim:
                return
            timeout = min(self.timeout - claim['spent_seconds'],
                          self.backend.c['command_timeout_seconds'] * 2 ** claim['attempts'], 1800)
            claim['attempt_deadline'] = time.time() + max(1, timeout)
            self.backend.claims[task.id] = claim
        try:
            super().task(task, base)
            rec = self.store.records()[task.id]
            resolved = rec['state'] == INTEGRATED
            if claim and resolved:
                self.queue.release(claim, resolved=True)
            if rec['state'] in {'BLOCKED', 'FAILED'}:
                self.record_failure(task, claimed=claim)
        finally:
            self.backend.claims.pop(task.id, None)

    def recover_interrupted(self):
        for tid, job in self.queue.records().items():
            if job['state'] not in {'CLAIMED', 'RELEASED'}:
                continue
            rec = self.store.records().get(tid, {})
            if process_alive(job.get('lease_pid')) or process_alive(rec.get('pid')) or rec.get('pending_launch'):
                continue
            resolved = rec.get('state') == INTEGRATED
            if resolved and job['state'] == 'CLAIMED':
                self.queue.release(job, resolved=True)
            if not resolved:
                self.store.update(tid, 'BLOCKED', reason='Previous recovery worker exited; preserve and recapture partial source')
                self.record_failure(self.plan.tasks[tid], claimed=job if job['state'] == 'CLAIMED' else None)


def run_recovery(config_path, approval_path, batch_path, recovery_path, wait=False):
    c = load_config(config_path)
    approved = require_approval(c, approval_path)
    request = batch_approval(c, batch_path, approved)
    policy = recovery_approval(c, recovery_path, request)
    plan, root = Plan(Path(c['repo']), c['pins']), Path(c['state_dir'])
    project_lock = Path(tempfile.gettempdir()) / 'autodispatch-locks' / digest(str(plan.repo).lower().encode())
    with service_lease(root, wait=wait, timeout=86400), CoordinatorLock(project_lock), CoordinatorLock(root):
        active = json.loads((root / 'batch-active.json').read_text(encoding='utf-8'))
        if active['batch_id'] != request['batch_id']:
            raise Blocked('Recovery cannot enter a different batch')
        store = Store(root)
        service = {'pid': os.getpid(), 'state': 'RUNNING', 'started_at': time.time(),
                   'workflow': 'integrate-first-validate-last', 'batch_id': request['batch_id'],
                   'phase': 'failure-recovery-enabled', 'max_workers': worker_capacity(c, plan, approved['authorized_tasks']),
                   'worker_mode': c['max_workers'],
                   'max_agent_requests': c['max_agent_requests'], 'recovery_max_attempts': policy['max_attempts']}
        atomic_json(root / 'service.json', service)
        try:
            backend = RecoveryBackend(c, plan, store)
            backend.batch_id, backend.request = request['batch_id'], request
            validate_spec(plan.repo, c['manifest_sha256'])
            queue = RecoveryQueue(store, max_attempts=policy['max_attempts'])
            engine = RecoveryEngine(plan, store, backend, approved['authorized_tasks'], c['task_timeout_seconds'], queue=queue)
            engine.recover_interrupted()
            # Adopt current failures once; queue attempts and snapshots survive restarts.
            for tid, rec in store.records().items():
                if tid in approved['authorized_tasks'] and rec['state'] in {'RUNNING', 'RECOVERY_RUNNING', 'CODE_READY_VERIFY_PENDING'}:
                    if process_alive(rec.get('pid')) or rec.get('pending_launch'):
                        store.update(tid, 'BLOCKED', reason='Ambiguous interrupted process; automatic restoration refused')
                        continue
                    store.update(tid, 'BLOCKED', reason='Interrupted coordinator; preserve and restore its current task source')
                    rec = store.records()[tid]
                if tid in approved['authorized_tasks'] and tid not in queue.records() and rec['state'] in {'BLOCKED', 'FAILED'}:
                    engine.record_failure(plan.tasks[tid])
            for _ in range(1 + len(plan.tasks) * 3):
                service.update(heartbeat_at=time.time())
                atomic_json(root / 'service.json', service)
                result = engine.run(c['max_workers'], c['max_tasks'])
                if result.get('worker_configuration_blocked') and not result['launched_tasks']:
                    service.update(state='BLOCKED', reason='Worker 模型配置不可用，请在界面修正后显式恢复')
                    break
                if result.get('budget_exhausted') or (root / 'stop.request').exists():
                    service.update(state='BUDGET_EXHAUSTED' if result.get('budget_exhausted') else 'STOPPED')
                    break
                records = store.records()
                incomplete = [tid for tid in approved['authorized_tasks']
                              if records.get(tid, {}).get('state') not in {'MERGED_VERIFIED', INTEGRATED}]
                if not incomplete:
                    service.update(phase='final-validation')
                    atomic_json(root / 'service.json', service)
                    report, validation = final_checks(backend, plan, approved['authorized_tasks'])
                    service.update(final_report=str(report))
                    for tid, rec in records.items():
                        if rec.get('batch_id') == request['batch_id'] and rec['state'] == INTEGRATED:
                            store.update(tid, batch_validation=validation['status'], batch_report=str(report))
                    # Targeted failures get one bounded recovery cycle, still respecting the same attempt cap.
                    targets = [x['name'] for x in validation['checks']
                               if x['status'] == 'FAIL' and x['name'] in plan.tasks and x['name'] not in {'IP-00', 'IP-34'}]
                    for tid in targets:
                        failed = next(x for x in validation['checks'] if x['name'] == tid)
                        store.update(tid, 'BLOCKED', reason='Final batch target failed; inspect exact-SHA report: ' + str(report),
                                     force_agent_repair=True, last_receipt=failed.get('receipt', {}))
                        engine.record_failure(plan.tasks[tid])
                    if not targets or not any(j['state'] == 'QUEUED' for j in queue.records().values()):
                        service.update(state=validation['status'])
                        break
                pending = [j for j in queue.records().values() if j['state'] == 'QUEUED']
                if not pending:
                    service.update(state='BLOCKED', incomplete_tasks=incomplete)
                    break
                now = time.time()
                if not result['launched_tasks'] and all(j['ready_at'] <= now for j in pending):
                    service.update(state='BLOCKED', incomplete_tasks=incomplete, reason='Queued recovery dependencies or ownership are blocked')
                    break
                delay = max(.2, min(30, min(j['ready_at'] for j in pending) - now))
                until = time.monotonic() + delay
                while time.monotonic() < until and not (root / 'stop.request').exists():
                    time.sleep(.2)
            else:
                service.update(state='BLOCKED', reason='Finite recovery dispatch limit reached')
        except Exception as exc:
            service.update(state='BLOCKED', reason=f'{type(exc).__name__}: {exc}')
            raise
        finally:
            store.close()
            service.update(finished_at=time.time())
            atomic_json(root / 'service.json', service)
    return service


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('config', 'approval', 'batch-approval', 'recovery-approval'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--execute', action='store_true', required=True)
    parser.add_argument('--wait-for-idle', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run_recovery(args.config, args.approval, args.batch_approval, args.recovery_approval,
                                  args.wait_for_idle), ensure_ascii=False))


if __name__ == '__main__':
    main()

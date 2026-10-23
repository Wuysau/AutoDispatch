"""Durable, atomically claimed recovery jobs using the dispatcher's SQLite store."""
from __future__ import annotations

import json
import os
import time
import uuid

from .model import Blocked
from .safety import redact


def retry_block_reason(job, record):
    if job['state'] != 'QUEUED' or record.get('state') != 'RECOVERY_QUEUED':
        return '仅排队中的失败任务可重试；运行中或需人工处理的任务不能重新领取'
    if job['attempts'] >= job['max_attempts'] or job.get('spent_seconds', 0) >= 3600:
        return '已达到恢复次数或累计执行时间上限'
    snapshot = job.get('snapshot')
    if not isinstance(snapshot, dict) or not snapshot.get('path') or not snapshot.get('sha256'):
        return '缺少可恢复的失败现场，请先检查任务证据'
    if (record.get('pid') or job.get('lease_pid') or record.get('pending_launch')
            or record.get('local_merge_intent') or record.get('writer_released') is False):
        return '任务仍持有执行现场，请等待当前 worker 释放'
    return None


def arrival_order(db, job):
    """Recover a legacy job's first queue event without changing its runtime state."""
    if 'created_seq' not in job:
        row = db.execute("SELECT MIN(seq) FROM events WHERE task=? "
                         "AND json_extract(payload,'$.recovery.jobId')=?",
                         (job['task_id'], job['id'])).fetchone()
        job['created_seq'] = row[0] or 0
    return job


def recovery_summary(job, record):
    reason = retry_block_reason(job, record)
    return {
        'jobId': job['id'], 'state': job['state'], 'attempts': job['attempts'],
        'maxAttempts': job['max_attempts'], 'nextAttemptAt': int(job['ready_at'] * 1000),
        'enqueuedAt': job['created_at'] * 1000, 'queueOrder': job.get('created_seq', 0),
        'manualPriority': job.get('manual_priority', 0),
        'canRetry': reason is None, 'retryBlockedReason': reason,
        'errorClass': job['error_class'], 'lastError': job['error'],
        'snapshot': job.get('snapshot'), 'spentSeconds': job.get('spent_seconds', 0),
    }


class RecoveryQueue:
    def __init__(self, store, max_attempts=2, backoff=30):
        self.store, self.maximum, self.backoff = store, max_attempts, backoff
        if not 1 <= max_attempts <= 3:
            raise Blocked("Recovery attempts must be between one and three")
        with store.lock, store.db:
            store.db.execute('CREATE TABLE IF NOT EXISTS recovery_jobs(task TEXT PRIMARY KEY, payload TEXT NOT NULL)')

    def records(self):
        with self.store.lock:
            return {tid: arrival_order(self.store.db, json.loads(raw)) for tid, raw in
                    self.store.db.execute('SELECT task,payload FROM recovery_jobs')}

    def _save(self, job, task_state=None, **fields):
        """Caller holds the SQLite transaction and store lock."""
        job['updated_at'] = time.time()
        self.store.db.execute('INSERT OR REPLACE INTO recovery_jobs VALUES(?,?)',
                              (job['task_id'], json.dumps(job)))
        row = self.store.db.execute('SELECT payload FROM tasks WHERE id=?', (job['task_id'],)).fetchone()
        rec = json.loads(row[0]) if row else {'task_id': job['task_id'], 'state': 'PENDING'}
        if task_state:
            rec['state'] = task_state
        rec.update(fields, updated_at=job['updated_at'])
        rec['recovery'] = recovery_summary(job, rec)
        raw = json.dumps(rec)
        self.store.db.execute('INSERT OR REPLACE INTO tasks VALUES(?,?)', (job['task_id'], raw))
        self.store.db.execute('INSERT INTO events(task,at,payload) VALUES(?,?,?)',
                              (job['task_id'], job['updated_at'], raw))

    def enqueue(self, tid, snapshot, error, error_class='EXECUTION', retryable=True,
                elapsed=0, receipt_key='', now=None, claimed=None):
        now = time.time() if now is None else now
        with self.store.lock, self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            row = self.store.db.execute('SELECT payload FROM recovery_jobs WHERE task=?', (tid,)).fetchone()
            job = json.loads(row[0]) if row else {
                'id': uuid.uuid4().hex, 'task_id': tid, 'attempts': 0,
                'max_attempts': self.maximum, 'created_at': now, 'charged_receipts': [], 'spent_seconds': 0,
                'created_seq': self.store.db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM events').fetchone()[0],
            }
            arrival_order(self.store.db, job)
            if job.get('state') == 'CLAIMED':
                if not claimed or claimed.get('lease_token') != job.get('lease_token'):
                    raise Blocked('Recovery job is still claimed')
            if receipt_key and receipt_key not in job['charged_receipts']:
                job['spent_seconds'] += max(0, elapsed)
                job['charged_receipts'].append(receipt_key)
            exhausted = job['attempts'] >= job['max_attempts'] or job['spent_seconds'] >= 3600
            state = 'BLOCKED' if not retryable else 'EXHAUSTED' if exhausted else 'QUEUED'
            job.update(state=state, snapshot=snapshot, error=redact(str(error))[-6000:],
                       error_class=error_class, lease_pid=None, lease_token=None,
                       manual_priority=0,
                       ready_at=now + (min(120, self.backoff * 2 ** (job['attempts'] - 1))
                                       if job['attempts'] else 0))
            task_state = 'RECOVERY_QUEUED' if state == 'QUEUED' else 'RECOVERY_EXHAUSTED' if exhausted else 'BLOCKED'
            reason = (f"失败恢复队列：待领取（已修复 {job['attempts']}/{job['max_attempts']} 次）" if state == 'QUEUED'
                      else f"自动恢复停止：{job['error_class']}；{job['error'][:300]}")
            fields = {'reason': reason}
            if retryable:
                fields.update(pid=None, pending_launch=False, writer_released=True)
            self._save(job, task_state, **fields)
            return job

    def ready(self, now=None):
        now = time.time() if now is None else now
        return sorted((j for j in self.records().values() if j['state'] == 'QUEUED' and j['ready_at'] <= now),
                      key=lambda j: (-j.get('manual_priority', 0), j['created_at'],
                                     j.get('created_seq', 0), j['task_id']))

    def prioritize(self, tid, now=None):
        """Move an existing retryable job to the front, without claiming or resetting it."""
        now = time.time() if now is None else now
        with self.store.lock, self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            row = self.store.db.execute('SELECT payload FROM recovery_jobs WHERE task=?', (tid,)).fetchone()
            if not row:
                raise Blocked('任务尚未进入失败恢复队列，请先检查失败现场')
            job = arrival_order(self.store.db, json.loads(row[0]))
            row = self.store.db.execute('SELECT payload FROM tasks WHERE id=?', (tid,)).fetchone()
            record = json.loads(row[0]) if row else {}
            reason = retry_block_reason(job, record)
            if reason:
                raise Blocked(reason)
            # Event sequence is durable and allocated under the same write transaction.
            priority = self.store.db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM events').fetchone()[0]
            job.update(manual_priority=priority, ready_at=min(now, job['ready_at']))
            self._save(job, reason='手动重试：已置于重试队列最前，等待空闲 worker')
            return job

    def claim(self, tid, now=None):
        now = time.time() if now is None else now
        with self.store.lock:
            try:
                self.store.db.execute('BEGIN IMMEDIATE')
                row = self.store.db.execute('SELECT payload FROM recovery_jobs WHERE task=?', (tid,)).fetchone()
                if not row:
                    self.store.db.rollback()
                    return None
                job = json.loads(row[0])
                if (job['state'] != 'QUEUED' or job['ready_at'] > now
                        or job['attempts'] >= job['max_attempts'] or job.get('spent_seconds', 0) >= 3600):
                    self.store.db.rollback()
                    return None
                job.update(state='CLAIMED', attempts=job['attempts'] + 1,
                           lease_pid=os.getpid(), lease_token=uuid.uuid4().hex, claimed_at=now, manual_priority=0)
                self._save(job, 'RECOVERY_RUNNING', writer_released=False,
                           reason=f"自动修复 {job['attempts']}/{job['max_attempts']}：恢复失败现场并继续执行")
                self.store.db.commit()
                return job
            except BaseException:
                self.store.db.rollback()
                raise

    def release(self, claimed, resolved=False):
        with self.store.lock, self.store.db:
            row = self.store.db.execute('SELECT payload FROM recovery_jobs WHERE task=?',
                                       (claimed['task_id'],)).fetchone()
            job = json.loads(row[0])
            if job.get('state') != 'CLAIMED' or job.get('lease_token') != claimed['lease_token']:
                raise Blocked('Recovery lease is no longer owned by this worker')
            job.update(state='RESOLVED' if resolved else 'RELEASED', lease_pid=None, lease_token=None)
            self._save(job)
            return job

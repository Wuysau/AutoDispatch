import { AlertOctagon, ArrowRight, GitBranch, Workflow, X } from 'lucide-react'
import { useApp } from './AppContext'
import { ProgressBar, StatusBadge } from './ui'
import { RetryButton } from './RetryButton'
import type { Task } from '../types'
import { ExecutionPanel } from './ExecutionPanel'
import { STATUS_META } from '../utils'

interface Props {
  task: Task
  onClose: () => void
  compact?: boolean
}

export function TaskDetail({ task, onClose, compact = false }: Props) {
  const { snapshot, selectTask, setTab } = useApp()
  const byId = new Map(snapshot.tasks.map((t) => [t.id, t]))
  const dependents = snapshot.tasks.filter((t) => t.deps.includes(task.id))
  const upstreamFailed = task.blockedBy?.filter((id) => byId.get(id)?.status === 'failed') ?? []

  return (
    <div className={`task-detail ${compact ? 'is-compact' : ''}`}>
      <div className="detail-head">
        <div>
          <div className="row-gap">
            <span className="task-id">{task.id}</span>
            <StatusBadge status={task.status} compact />
          </div>
          <h3 className="detail-title">{task.name}</h3>
        </div>
        <button className="icon-btn" aria-label="关闭详情" onClick={onClose}>
          <X size={15} />
        </button>
      </div>

      <div className="detail-progress">
        <div className="row-between small">
          <span className="muted">{task.progress === null ? '阶段' : '进度'}</span>
          <span className="num">{task.progress === null ? task.stage : `${Math.round(task.progress)}%`}</span>
        </div>
        <ProgressBar value={task.progress} tone={STATUS_META[task.status].tone} />
      </div>

      <dl className="kv">
        <dt>执行器</dt>
        <dd>{task.executor}</dd>
        <dt>阶段</dt>
        <dd>{task.stage}</dd>
        <dt>工作树</dt>
        <dd className="mono">{task.worktree ?? '—'}</dd>
        <dt>状态</dt>
        <dd>{STATUS_META[task.status].label}</dd>
        {task.recovery && <>
          <dt>失败恢复</dt>
          <dd>{{ QUEUED: '排队等待空闲 worker', CLAIMED: '已领取，正在修复', RESOLVED: '恢复完成',
            EXHAUSTED: '已达到自动修复上限', BLOCKED: '需要人工处理', RELEASED: '等待记录本次结果' }[task.recovery.state]}</dd>
          <dt>修复次数</dt>
          <dd>{task.recovery.attempts} / {task.recovery.maxAttempts}</dd>
          <dt>失败类型</dt>
          <dd>{task.recovery.errorClass}</dd>
          {!!task.recovery.manualPriority && <><dt>重试顺序</dt><dd>手动优先，等待调度</dd></>}
        </>}
      </dl>

      <ExecutionPanel key={task.id} task={task} />

      {task.blockedReason && (
        <div className={`notice ${upstreamFailed.length ? 'notice-error' : 'notice-warn'}`}>
          <AlertOctagon size={15} />
          <div>
            <strong>{task.recovery ? '恢复进度' : '阻塞原因'}</strong>
            <div className="small">{task.blockedReason}</div>
          </div>
        </div>
      )}

      {task.lastError && (
        <div className="notice notice-error">
          <AlertOctagon size={15} />
          <div>
            <strong>最近错误</strong>
            <div className="small mono">{task.lastError}</div>
          </div>
        </div>
      )}

      {task.recovery && task.recovery.state !== 'RESOLVED' && (
        <div className="notice notice-warn">
          <AlertOctagon size={15} />
          <div style={{ minWidth: 0 }}>
            <strong>最近失败</strong>
            <div className="small" style={{ overflowWrap: 'anywhere' }}>
              {task.recovery.errorClass === 'TIMEOUT'
                ? '执行超时。已保存源码和日志，本轮从保留的进度继续。'
                : task.recovery.lastError.split('\n')[0].slice(0, 240)}
            </div>
            <details className="small">
              <summary>原始错误</summary>
              <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 240, overflow: 'auto' }}>
                {task.recovery.lastError}
              </pre>
            </details>
          </div>
        </div>
      )}

      <section className="detail-section">
        <div className="section-title">
          <GitBranch size={13} /> 上游依赖 ({task.deps.length})
        </div>
        {task.deps.length === 0 && <div className="muted small">无（根任务）</div>}
        <div className="link-list">
          {task.deps.map((d) => (
            <DepLink key={d} id={d} status={byId.get(d)?.status} onClick={() => selectTask(d)} />
          ))}
        </div>
      </section>

      <section className="detail-section">
        <div className="section-title">
          <ArrowRight size={13} /> 下游任务 ({dependents.length})
        </div>
        {dependents.length === 0 && <div className="muted small">无</div>}
        <div className="link-list">
          {dependents.map((d) => (
            <DepLink key={d.id} id={d.id} status={d.status} onClick={() => selectTask(d.id)} />
          ))}
        </div>
      </section>

      <div className="detail-actions">
        <RetryButton task={task} />
        {!compact && (
          <button className="btn" onClick={() => setTab('dag')}>
            <Workflow size={14} /> 在 DAG 中查看
          </button>
        )}
      </div>
      {task.recovery?.retryBlockedReason && task.recovery.state !== 'RESOLVED' && (
        <div className="small muted">{task.recovery.retryBlockedReason}</div>
      )}
    </div>
  )
}

function DepLink({ id, status, onClick }: { id: string; status?: Task['status']; onClick: () => void }) {
  return (
    <button className="dep-link" onClick={onClick}>
      <span className="task-id">{id}</span>
      {status && <StatusBadge status={status} compact />}
    </button>
  )
}

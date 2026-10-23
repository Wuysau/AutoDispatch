import { useMemo, useState } from 'react'
import { Search, X } from 'lucide-react'
import { useApp } from './AppContext'
import { EmptyState, ProgressBar, StatusBadge } from './ui'
import { RetryButton } from './RetryButton'
import { compareFailureArrival, countBy, STATUS_META } from '../utils'
import type { Executor, TaskStatus } from '../types'

const FILTERS: (TaskStatus | 'all')[] = ['all', 'running', 'pending', 'waiting', 'completed', 'failed']

export function TasksView() {
  const { snapshot, selectedTaskId, selectTask, statusFilter, setStatusFilter } = useApp()
  const [query, setQuery] = useState('')
  const [executor, setExecutor] = useState<Executor | 'all'>('all')
  const [recoveryOnly, setRecoveryOnly] = useState(false)
  const recoveryCount = snapshot.tasks.filter((t) => t.recovery && t.recovery.state !== 'RESOLVED').length

  const counts = countBy(snapshot.tasks)

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase()
    const filtered = snapshot.tasks.filter((t) => {
      if (recoveryOnly && (!t.recovery || t.recovery.state === 'RESOLVED')) return false
      if (statusFilter !== 'all' && t.status !== statusFilter) return false
      if (executor !== 'all' && t.executor !== executor) return false
      if (!q) return true
      return t.id.toLowerCase().includes(q) || t.name.toLowerCase().includes(q) || t.stage.toLowerCase().includes(q)
    })
    return recoveryOnly || statusFilter === 'failed' ? filtered.sort(compareFailureArrival) : filtered
  }, [snapshot.tasks, statusFilter, executor, query, recoveryOnly])

  const hasAny = snapshot.tasks.length > 0

  return (
    <div className="tasks-view">
      <div className="toolbar">
        <div className="search">
          <Search size={14} />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索任务 ID、名称或阶段"
            aria-label="搜索任务"
          />
          {query && (
            <button className="icon-btn icon-btn-sm" aria-label="清空搜索" onClick={() => setQuery('')}>
              <X size={12} />
            </button>
          )}
        </div>
        <select
          className="select"
          value={executor}
          onChange={(e) => setExecutor(e.target.value as Executor | 'all')}
          aria-label="按执行器过滤"
        >
          <option value="all">全部执行器</option>
          <option value="Codex">Codex</option>
          <option value="OpenCode">OpenCode</option>
        </select>
      </div>

      <div className="filter-row" role="group" aria-label="按状态过滤">
        {FILTERS.map((f) => {
          const label = f === 'all' ? '全部' : STATUS_META[f].label
          const n = f === 'all' ? snapshot.tasks.length : counts[f]
          return (
            <button
              key={f}
              className={`filter-chip ${statusFilter === f && !recoveryOnly ? 'is-active' : ''}`}
              onClick={() => { setRecoveryOnly(false); setStatusFilter(f) }}
            >
              {label} <span className="num">{n}</span>
            </button>
          )
        })}
        <button
          className={`filter-chip ${recoveryOnly ? 'is-active' : ''}`}
          aria-pressed={recoveryOnly}
          onClick={() => { setStatusFilter('all'); setRecoveryOnly(true) }}
        >
          失败恢复 <span className="num">{recoveryCount}</span>
        </button>
        {(recoveryOnly || statusFilter === 'failed') && <span className="small muted">按首次进入失败队列排序</span>}
      </div>

      {!hasAny ? (
        <EmptyState title="暂无任务" hint="调度计划尚未加载，请先运行 plan 生成任务图" />
      ) : rows.length === 0 ? (
        <EmptyState title="没有匹配的任务" hint="调整搜索词或过滤条件" />
      ) : (
        <div className="table-wrap">
          <table className="task-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>任务</th>
                <th>执行器</th>
                <th>状态</th>
                <th>进度</th>
                <th>依赖</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((t) => (
                <tr
                  key={t.id}
                  className={selectedTaskId === t.id ? 'is-selected' : ''}
                  onClick={() => selectTask(t.id)}
                  tabIndex={0}
                  onKeyDown={(e) => e.target === e.currentTarget && e.key === 'Enter' && selectTask(t.id)}
                >
                  <td className="task-id">{t.id}</td>
                  <td>
                    <div>{t.name}</div>
                    <div className="small muted">{t.stage}</div>
                    {!!t.recovery?.manualPriority && <div className="small text-blue">手动优先</div>}
                  </td>
                  <td className={t.executor === 'Codex' ? 'text-blue' : 'text-violet'}>{t.executor}</td>
                  <td>
                    <StatusBadge status={t.status} compact />
                  </td>
                  <td className="progress-cell">
                    <ProgressBar value={t.progress} tone={STATUS_META[t.status].tone} />
                    <span className="num small muted">{t.progress === null ? '未知' : `${Math.round(t.progress)}%`}</span>
                  </td>
                  <td className="num muted small">{t.deps.length ? t.deps.join(', ') : '—'}</td>
                  <td className="retry-cell"><RetryButton task={t} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

import { useMemo, useState } from 'react'
import { Download, Search, X } from 'lucide-react'
import { useApp } from './AppContext'
import { EmptyState } from './ui'
import { formatClock, LEVEL_TONE, newestLogs } from '../utils'
import type { LogLevel } from '../types'

const LEVELS: (LogLevel | 'all')[] = ['all', 'INFO', 'SUCCESS', 'WARN', 'ERROR']

export function LogsView() {
  const { snapshot, selectTask, notify } = useApp()
  const [level, setLevel] = useState<LogLevel | 'all'>('all')
  const [query, setQuery] = useState('')

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase()
    return newestLogs(snapshot.logs).filter((l) => {
      if (level !== 'all' && l.level !== level) return false
      if (!q) return true
      return `${l.message} ${l.source} ${l.taskId ?? ''}`.toLowerCase().includes(q)
    })
  }, [snapshot.logs, level, query])

  const exportLogs = () => {
    if (rows.length === 0) {
      notify('没有可导出的日志', 'warn')
      return
    }
    const text = rows
      .map((l) => `${new Date(l.time).toISOString()} [${l.level}] ${l.source}${l.taskId ? ` ${l.taskId}` : ''} ${l.message}`)
      .join('\n')
    const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }))
    const a = document.createElement('a')
    a.href = url
    a.download = `autodispatch-logs-${snapshot.source}.txt`
    a.click()
    URL.revokeObjectURL(url)
    notify(`已导出 ${rows.length} 条日志`, 'success')
  }

  return (
    <div className="logs-view">
      <div className="toolbar">
        <div className="search">
          <Search size={14} />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索日志内容、来源或任务 ID"
            aria-label="搜索日志"
          />
          {query && (
            <button className="icon-btn icon-btn-sm" aria-label="清空搜索" onClick={() => setQuery('')}>
              <X size={12} />
            </button>
          )}
        </div>
        <button className="btn btn-sm" onClick={exportLogs}>
          <Download size={13} /> 导出
        </button>
      </div>

      <div className="filter-row" role="group" aria-label="按级别过滤">
        {LEVELS.map((l) => (
          <button key={l} className={`filter-chip ${level === l ? 'is-active' : ''}`} onClick={() => setLevel(l)}>
            {l === 'all' ? '全部' : l}
          </button>
        ))}
        <span className="muted small grow right">
          最新在前 · {rows.length} / {snapshot.logs.length} 条
        </span>
      </div>

      {snapshot.logs.length === 0 ? (
        <EmptyState title="暂无日志" hint="调度器尚未产生执行记录" />
      ) : rows.length === 0 ? (
        <EmptyState title="没有匹配的日志" hint="调整级别或搜索词" />
      ) : (
        <div className="log-list">
          {rows.map((l) => (
            <div key={l.id} className="log-entry">
              <span className="mono muted">{formatClock(l.time)}</span>
              <span className={`log-level tone-text-${LEVEL_TONE[l.level]} mono`}>[{l.level}]</span>
              <span className="mono muted log-source">{l.source}</span>
              <span className="grow log-message">{l.message}</span>
              {l.taskId && (
                <button className="link-btn small" onClick={() => selectTask(l.taskId!)}>
                  {l.taskId}
                </button>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

import { useEffect, useRef, useState } from 'react'
import { Activity, RefreshCw } from 'lucide-react'
import type { ExecutionDetail, ExecutionEvent, ExecutionSummary, Task } from '../types'
import { useApp } from './AppContext'
import { formatDuration } from '../utils'

export const processLabel = (state: ExecutionSummary['processState']) => ({
  starting: '正在启动', alive: '进程存活', exited: '进程已退出', finished: '命令已结束',
  between_commands: '协调器处理中', unknown: '进程状态未知',
}[state])

export function activityAge(time: number | null, now = Date.now()) {
  if (!time) return '尚无记录'
  const seconds = Math.max(0, Math.floor((now - time) / 1000))
  return seconds < 2 ? '刚刚' : `${formatDuration(seconds)}前`
}

function timestamp(time: number | null) {
  return time ? new Date(time).toLocaleString('zh-CN', { hour12: false }) : '尚无记录'
}

export function ExecutionPanel({ task }: { task: Task }) {
  const { adapter, snapshot } = useApp()
  const [detail, setDetail] = useState<ExecutionDetail | null>(null)
  const [events, setEvents] = useState<ExecutionEvent[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [follow, setFollow] = useState(true)
  const [query, setQuery] = useState('')
  const [refresh, setRefresh] = useState(0)
  const [now, setNow] = useState(Date.now())
  const log = useRef<HTMLDivElement>(null)
  const cursor = useRef(0)
  const attempt = useRef<string | null>(null)

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    let disposed = false
    let timer: number | undefined
    let controller: AbortController | undefined
    let busy = false
    const poll = async () => {
      if (disposed || document.hidden || busy) return
      busy = true
      controller = new AbortController()
      const timeout = window.setTimeout(() => controller?.abort(), 8000)
      try {
        const next = await adapter.loadExecution(task.id, cursor.current, attempt.current, controller.signal)
        if (disposed) return
        const changed = next.execution.id !== attempt.current
        attempt.current = next.execution.id
        cursor.current = next.cursor
        setDetail(next)
        setEvents(old => {
          const rows = changed ? [] : old
          const known = new Set(rows.map(row => row.seq))
          return [...rows, ...next.events.filter(row => !known.has(row.seq))].slice(-300)
        })
        setError('')
      } catch (err) {
        if (!disposed) setError(err instanceof Error ? err.message : '读取执行详情失败')
      } finally {
        window.clearTimeout(timeout)
        busy = false
        if (!disposed) {
          setLoading(false)
          timer = window.setTimeout(() => void poll(), 1500)
        }
      }
    }
    const visible = () => {
      if (document.hidden) controller?.abort()
      else {
        window.clearTimeout(timer)
        void poll()
      }
    }
    document.addEventListener('visibilitychange', visible)
    void poll()
    return () => {
      disposed = true
      window.clearTimeout(timer)
      controller?.abort()
      document.removeEventListener('visibilitychange', visible)
    }
  }, [adapter, task.id, refresh])

  useEffect(() => {
    if (follow && log.current) log.current.scrollTop = log.current.scrollHeight
  }, [events, follow, query])

  const execution = detail?.execution ?? task.execution
  const rows = events.filter(row => `${row.message} ${row.detail}`.toLowerCase().includes(query.toLowerCase()))
  const last = execution?.lastActivityAt ?? execution?.lastOutputAt
  const silence = execution?.lastOutputAt ?? execution?.startedAt
  const quiet = execution?.processState === 'alive' && silence && now - silence > 120_000

  return (
    <section className="execution-panel" aria-label="Worker 执行详情">
      <div className="row-between">
        <strong className="row-gap"><Activity size={14} />执行详情</strong>
        <button className="icon-btn icon-btn-sm" title="刷新执行详情" aria-label="刷新执行详情" onClick={() => setRefresh(n => n + 1)}>
          <RefreshCw size={13} />
        </button>
      </div>
      {snapshot.source === 'demo' && <p className="small tone-text-amber">DEMO · 以下操作和进程为模拟数据</p>}
      {loading && !execution && <p className="small muted" role="status">正在读取执行详情…</p>}
      {error && <p className="small tone-text-amber" role="status">{error}；已有记录可能过期，自动重连中。</p>}
      {execution && <>
        <dl className="kv execution-kv">
          <dt>受管进程</dt>
          <dd>{processLabel(execution.processState)}{execution.pid ? <span className="mono"> · PID {execution.pid}</span> : ''}</dd>
          <dt>当前阶段</dt><dd>{task.status === 'running' ? execution.stage : task.stage}</dd>
          <dt>命令耗时</dt><dd className="num">{execution.startedAt ? formatDuration(Math.max(0, Math.floor(((execution.finishedAt ?? now) - execution.startedAt) / 1000))) : '尚无记录'}</dd>
          <dt>最近活动</dt><dd title={timestamp(last ?? null)}>{timestamp(last ?? null)}<span className="muted"> · {activityAge(last ?? null, now)}</span></dd>
          <dt>最近输出</dt><dd title={timestamp(execution.lastOutputAt)}>{activityAge(execution.lastOutputAt, now)}</dd>
          <dt>{execution.finishedAt ? '最后操作' : '当前操作'}</dt><dd>{execution.currentOperation ?? '尚未收到操作事件'}</dd>
          {execution.finishedAt && <><dt>命令结果</dt><dd>exit {execution.exitCode} · {timestamp(execution.finishedAt)}</dd></>}
        </dl>
        <p className="small muted">进程存活不代表持续推进；活动时间来自实际输出事件。命令结束不代表任务验收通过。</p>
        {quiet && <p className="small tone-text-amber" role="status">已超过 2 分钟未收到输出。进程仍存活，是否阻塞尚无法确认。</p>}
        {execution.telemetry === 'unavailable' && <p className="small muted">本次命令未启用实时采集，或采集记录不可用。旧执行器的日志仅在命令结束后落盘。</p>}
        {execution.id && <div className="small muted execution-attempt">本次命令 <span className="mono">{execution.id.slice(0, 12)}</span> · {execution.outputBytes.toLocaleString()} bytes</div>}
      </>}
      <div className="execution-toolbar">
        <label className="small"><input type="checkbox" checked={follow} onChange={event => setFollow(event.target.checked)} />跟随最新</label>
        <input className="execution-search" type="search" placeholder="筛选本次日志" aria-label="筛选本次执行日志" value={query} onChange={event => setQuery(event.target.value)} />
      </div>
      {detail?.truncated && <p className="small muted">仅显示最近事件，较早日志保留在本次执行记录中。</p>}
      <div className="execution-log" ref={log} tabIndex={0} aria-label="本次命令实时日志">
        {!rows.length && <p className="small muted">{query ? '没有匹配的日志' : '尚无日志事件。此处仅显示本次命令，不混入历史失败。'}</p>}
        {rows.map(row => (
          <details className="execution-event" key={row.seq}>
            <summary><time className="mono muted">{new Date(row.time).toLocaleTimeString('zh-CN', { hour12: false })}</time><span className="execution-event-message">{row.message}</span></summary>
            <pre>{row.detail || row.message}</pre>
          </details>
        ))}
      </div>
    </section>
  )
}

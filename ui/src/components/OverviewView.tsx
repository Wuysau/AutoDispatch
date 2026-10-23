import { lazy, Suspense, useMemo } from 'react'
import { AlertTriangle, ArrowRight, ChevronRight, Pause, Play, Square } from 'lucide-react'
import { useApp } from './AppContext'
import { EmptyState, Glass, LoadingState, ProgressBar } from './ui'
import { compareFailureArrival, countBy, focusTasks, formatClock, formatDuration, LEVEL_TONE, newestLogs, percent } from '../utils'
import type { TaskStatus } from '../types'
import { WorkerCard } from './WorkerCard'

const DagOverview = lazy(() => import('./DagView').then((m) => ({ default: m.DagOverview })))

export function OverviewView() {
  const { snapshot, adapter, runAction, setTab, selectTask, setStatusFilter } = useApp()
  const tasks = snapshot.tasks
  const counts = countBy(tasks)
  const total = tasks.length
  const progress = percent(counts.completed, total)
  const paused = snapshot.runState === 'paused'
  const stopped = snapshot.runState === 'stopped'
  const frontier = useMemo(() => focusTasks(tasks), [tasks])
  const recentLogs = useMemo(() => newestLogs(snapshot.logs).slice(0, 5), [snapshot.logs])

  const stats: { key: TaskStatus; label: string; tone: string; value: number }[] = [
    { key: 'running', label: 'Running', tone: 'green', value: counts.running },
    { key: 'pending', label: 'Pending', tone: 'blue', value: counts.pending },
    { key: 'waiting', label: 'Waiting', tone: 'gray', value: counts.waiting },
    { key: 'failed', label: 'Failed', tone: 'red', value: counts.failed },
  ]

  const goTasks = (status: TaskStatus) => {
    setStatusFilter(status)
    setTab('tasks')
  }

  return (
    <div className="overview">
      {counts.failed > 0 && (
        <div className="notice notice-error">
          <AlertTriangle size={15} />
          <div className="grow">
            检测到 {counts.failed} 个失败任务，下游任务已进入阻塞状态。
          </div>
          <button
            className="link-btn"
            onClick={() => {
              const f = tasks.filter((t) => t.status === 'failed').sort(compareFailureArrival)[0]
              if (f) selectTask(f.id)
              goTasks('failed')
            }}
          >
            查看失败任务 <ChevronRight size={13} />
          </button>
        </div>
      )}

      <div className="grid-top">
        <Glass className="card progress-card">
          <div className="card-label">Overall Progress</div>
          <div className="row-between">
            <div>
              <span className="big num">{counts.completed}</span>
              <span className="muted"> / {total} tasks</span>
            </div>
            <span className="num muted">{progress}%</span>
          </div>
          <ProgressBar value={progress} tone="green" />
        </Glass>

        <Glass className="card stats-card">
          {stats.map((s) => (
            <button key={s.key} className="stat" onClick={() => goTasks(s.key)} title={`查看${s.label}任务`}>
              <span className={`stat-ring tone-${s.tone}`}>{s.value}</span>
              <span className="stat-label">{s.label}</span>
            </button>
          ))}
        </Glass>

        <Glass className="card runtime-card">
          <div className="row-between">
            <div>
              <div className="card-label">Runtime</div>
              <div className="big num">{formatDuration(snapshot.runtimeSec)}</div>
            </div>
            <div className="btn-row">
              <button
                className="icon-btn icon-btn-lg"
                aria-label={paused ? '继续' : '暂停'}
                title={paused ? '继续' : '暂停'}
                disabled={stopped}
                onClick={() => runAction(() => adapter.control(paused ? 'resume' : 'pause'), paused ? '已继续调度' : '已暂停调度')}
              >
                {paused ? <Play size={17} /> : <Pause size={17} />}
              </button>
              <button
                className="icon-btn icon-btn-lg icon-btn-danger"
                aria-label="停止"
                title="停止"
                disabled={stopped}
                onClick={() => runAction(() => adapter.control('stop'), '已发送停止请求')}
              >
                <Square size={14} fill="currentColor" />
              </button>
            </div>
          </div>
          <div className={`small status-line tone-text-${snapshot.runState === 'running' ? 'green' : snapshot.runState === 'paused' ? 'amber' : 'gray'}`}>
            <span className="dot" />
            {snapshot.runState === 'running' ? 'Running' : snapshot.runState === 'paused' ? 'Paused' : 'Stopped'}
          </div>
        </Glass>
      </div>

      <Glass className="card workers-card">
        <div className="row-between">
          <div className="section-heading">Active Workers ({snapshot.workers.length} / {snapshot.concurrency})</div>
          <button className="link-btn small" onClick={() => runAction(() => adapter.refreshModels(), '已请求刷新官方模型目录')}>刷新模型目录</button>
        </div>
        <div className="small muted">{snapshot.workerMode === 'auto' ? '随 DAG 自动伸缩' : '当前配置限额'} · DAG 最大并行 {snapshot.dagMaxParallel}</div>
        {snapshot.workers.length === 0 && <div className="worker-empty small muted">暂无执行中的任务；调度器派发任务时会自动分配 Worker。</div>}
        <div className="worker-grid">
          {snapshot.workers.map(w => <WorkerCard key={`${w.slot}:${w.executor}`} worker={w} />)}
        </div>
        {snapshot.workerIssues.length > 0 && <>
          <div className="small tone-text-amber">以下 Worker 需要修正模型配置，当前未占用执行名额。</div>
          <div className="worker-grid">{snapshot.workerIssues.map(w => <WorkerCard key={`issue:${w.slot}:${w.executor}`} worker={w} />)}</div>
        </>}
      </Glass>

      <Glass className="card dag-card">
        <Suspense fallback={<LoadingState text="正在加载 DAG 模块…" />}>
          <DagOverview tasks={frontier} />
        </Suspense>
      </Glass>

      <Glass className="card recent-logs-card">
        <div className="row-between">
          <div className="section-heading">Recent Logs</div>
          <button className="link-btn" onClick={() => setTab('logs')}>
            View All <ArrowRight size={15} />
          </button>
        </div>
        {snapshot.logs.length === 0 ? (
          <EmptyState title="暂无日志" hint="调度器启动后会在此显示执行记录" />
        ) : (
          <div className="log-mini">
            {recentLogs.map((l) => (
              <div key={l.id} className="log-row">
                <span className={`dot tone-dot-${LEVEL_TONE[l.level]}`} />
                <span className="mono muted">{formatClock(l.time)}</span>
                <span className={`mono tone-text-${LEVEL_TONE[l.level]}`}>[{l.level}]</span>
                <span className="grow ellipsis">{l.message}</span>
              </div>
            ))}
          </div>
        )}
      </Glass>
    </div>
  )
}

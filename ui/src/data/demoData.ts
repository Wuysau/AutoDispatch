import type { DataAdapter, ExecutionDetail, ExecutionSummary, Executor, LogEntry, ModelCatalog, Snapshot, Task, TaskStatus, Worker, WorkerProfile } from '../types'
import { compareRetryPriority, dagMaxParallel } from '../utils'

const NAMES: string[] = [
  'Specification Baseline',
  'Domain Contracts',
  'Nautilus Adapter',
  'Data Pipeline',
  'Backtest Engine',
  'Strategy Framework',
  'Risk Management',
  'Paper Trading',
  'Broker Adapter',
  'Live Trading',
  'Instrument Registry',
  'Bar Aggregation',
  'Order Routing',
  'Position Ledger',
  'Fee Model',
  'Slippage Model',
  'Calendar Service',
  'Session Guard',
  'Reporting Core',
  'Metrics Export',
  'Alert Channel',
  'Config Loader',
  'Secrets Vault (mock)',
  'Audit Trail',
  'Replay Harness',
  'Stress Scenarios',
  'Walk-forward Runner',
  'Parameter Sweep',
  'Dashboard Feed',
  'Log Shipper',
  'Health Probes',
  'Cache Layer',
  'Migration Scripts',
  'CLI Surface',
  'Packaging',
  'Integration Smoke',
  'Documentation Pack',
  'Release Gate',
  'Handoff Report',
]

const EXECUTORS_OPENCODE = new Set(['IP-02', 'IP-08', 'IP-14', 'IP-21', 'IP-30'])
const STAGES = ['Specification', 'Red tests', 'Implementing', 'Reviewing', 'Testing', 'Merging']

function buildTaskSkeleton(): Task[] {
  const explicit: Record<number, number[]> = {
    0: [],
    1: [0],
    2: [0],
    3: [0],
    4: [1],
    5: [1, 2],
    6: [2, 3],
    7: [4, 5],
    8: [5, 6],
    9: [6],
  }
  return NAMES.map((name, i) => {
    const id = `IP-${String(i).padStart(2, '0')}`
    let deps = explicit[i]
    if (!deps) {
      const a = Math.max(0, i - 1 - (i % 4))
      const b = Math.max(0, i - 3)
      deps = [...new Set([a, b])].filter((d) => d < i)
      if (deps.length === 0) deps = [i - 1]
    }
    return {
      id,
      name,
      status: 'pending' as TaskStatus,
      executor: (EXECUTORS_OPENCODE.has(id) ? 'OpenCode' : 'Codex') as Executor,
      stage: STAGES[0],
      deps: deps.map((d) => `IP-${String(d).padStart(2, '0')}`),
      progress: 0,
      updatedAt: 0,
    }
  })
}

const FAIL_TASK_ID = 'IP-13'

/**
 * 纯前端 Demo 调度引擎。它只模拟界面状态流转，不调用真实 AutoDispatch，
 * 所有数据都会在界面顶部明确标注为 DEMO。
 */
export class DemoEngine implements DataAdapter {
  name = 'Demo（模拟数据）'
  private tasks: Task[]
  private logs: LogEntry[] = []
  private runState: Snapshot['runState'] = 'running'
  private runtimeSec = 28 * 60 + 16
  private listeners = new Set<(s: Snapshot) => void>()
  private timer: number | null = null
  private failedOnce = false
  private logSeq = 0
  private retryPriority = 0
  private startedAt: Record<string, number> = {}
  private readonly concurrency: number
  private assignments: Record<string, { slot: number; profile: WorkerProfile }> = {}
  private workerPrefs: Record<string, WorkerProfile> = {}
  private catalogs: Record<Executor, ModelCatalog> = {
    Codex: { status: 'loading', source: '', models: [] },
    OpenCode: { status: 'loading', source: '', models: [] },
  }
  private catalogPending = false
  private catalogReadAt = 0
  private snapshot: Snapshot

  constructor() {
    this.tasks = buildTaskSkeleton()
    this.concurrency = dagMaxParallel(this.tasks)
    const now = Date.now()
    this.setTask('IP-00', { status: 'completed', progress: 100, stage: 'Merged', updatedAt: now - 600_000 })
    this.setTask('IP-01', { status: 'running', progress: 42, stage: 'Implementing', worktree: 'AetherTrade-ip01', updatedAt: now })
    this.setTask('IP-02', { status: 'running', progress: 67, stage: 'Testing', executor: 'OpenCode', worktree: 'AetherTrade-ip02', updatedAt: now })
    this.startedAt['IP-01'] = now - 12 * 60_000 - 34_000
    this.startedAt['IP-02'] = now - 8 * 60_000 - 21_000
    this.setTask('IP-03', { status: 'pending', stage: 'Specification', updatedAt: now })
    this.assignWorker(this.tasks.find(t => t.id === 'IP-01')!)
    this.assignWorker(this.tasks.find(t => t.id === 'IP-02')!)
    this.log('SUCCESS', 'Task scheduler started', undefined, 'scheduler', now - 262_000)
    this.log('INFO', 'IP-00 Specification Baseline merged', 'IP-00', 'scheduler', now - 260_000)
    this.log('INFO', 'Codex Worker: IP-01 implementing domain contracts…', 'IP-01', 'Codex', now - 143_000)
    this.log('INFO', 'OpenCode Worker: IP-02 testing nautilus adapter…', 'IP-02', 'OpenCode', now - 40_000)
    this.snapshot = this.compute()
    this.timer = window.setInterval(() => this.tick(), 1000)
  }

  private setTask(id: string, patch: Partial<Task>) {
    this.tasks = this.tasks.map((t) => (t.id === id ? { ...t, ...patch } : t))
  }

  private preferred(slot: number, executor: Executor): WorkerProfile {
    return this.workerPrefs[`${slot}:${executor}`] ?? { model: null, reasoningEffort: null }
  }

  private assignWorker(task: Task) {
    const used = new Set(Object.values(this.assignments).map(assignment => assignment.slot))
    let slot = 1
    while (used.has(slot)) slot += 1
    this.assignments[task.id] = { slot, profile: { ...this.preferred(slot, task.executor) } }
  }

  private log(level: LogEntry['level'], message: string, taskId?: string, source = 'scheduler', time = Date.now()) {
    this.logSeq += 1
    this.logs = [{ id: `log-${this.logSeq}`, time, level, source, message, taskId }, ...this.logs].slice(0, 300)
  }

  /** 内部保存原始状态；pending 且依赖未满足时，对外派生为 waiting 并给出阻塞原因。 */
  private derive(): Task[] {
    const byId = new Map(this.tasks.map((t) => [t.id, t]))
    return this.tasks.map((t) => {
      if (t.status !== 'pending' && t.recovery?.state !== 'QUEUED') return { ...t, blockedBy: undefined, blockedReason: undefined }
      const failed = t.deps.filter((d) => byId.get(d)?.status === 'failed')
      const unmet = t.deps.filter((d) => byId.get(d)?.status !== 'completed')
      if (failed.length) {
        return { ...t, status: 'waiting' as TaskStatus, blockedBy: failed, blockedReason: `上游 ${failed.join(', ')} 失败，需先修复或重试` }
      }
      if (unmet.length) {
        return { ...t, status: 'waiting' as TaskStatus, blockedBy: unmet, blockedReason: `等待依赖 ${unmet.join(', ')} 完成` }
      }
      return { ...t, blockedBy: undefined, blockedReason: undefined }
    })
  }

  private compute(): Snapshot {
    const tasks = this.derive().map(t => ({ ...t,
      progress: t.status === 'completed' ? 100 : t.status === 'pending' ? 0 : null,
      execution: this.startedAt[t.id] ? this.executionSummary(t) : undefined,
    }))
    const running = tasks.filter((t) => t.status === 'running')
    const workers: Worker[] = running.map(t => {
      const assignment = this.assignments[t.id]
      return {
        slot: assignment.slot, executor: t.executor, taskId: t.id,
        elapsedSec: Math.floor((Date.now() - (this.startedAt[t.id] ?? Date.now())) / 1000),
        profile: assignment.profile, nextProfile: this.preferred(assignment.slot, t.executor),
        modelOptions: this.catalogs[t.executor].status === 'ready'
          ? [{ model: null, reasoningEfforts: [null] }, ...this.catalogs[t.executor].models] : [],
        configurable: this.catalogs[t.executor].status === 'ready',
        catalogStatus: this.catalogs[t.executor].status, catalogSource: this.catalogs[t.executor].source,
        catalogUpdatedAt: this.catalogs[t.executor].updatedAt, catalogError: this.catalogs[t.executor].error,
      }
    })
    return {
      source: 'demo',
      project: 'AetherTrade',
      subtitle: 'AI Development Orchestrator',
      runState: this.runState,
      runtimeSec: this.runtimeSec,
      concurrency: this.concurrency,
      dagMaxParallel: this.concurrency,
      workerMode: 'auto',
      tasks,
      workers,
      workerIssues: [],
      logs: this.logs,
      updatedAt: Date.now(),
    }
  }

  private emit() {
    this.snapshot = this.compute()
    this.listeners.forEach((l) => l(this.snapshot))
  }

  private tick() {
    const loadingCatalog = Object.values(this.catalogs).some(catalog => catalog.status === 'loading')
    if (Date.now() - this.catalogReadAt > (loadingCatalog ? 2000 : 30000)) void this.readCatalogs()
    if (this.runState !== 'running') return
    this.runtimeSec += 1
    let changed = false

    for (const t of [...this.tasks]) {
      if (t.status !== 'running') continue
      const step = 2 + Math.floor(Math.random() * 4)
      const next = Math.min(100, (t.progress ?? 0) + step)
      if (t.id === FAIL_TASK_ID && !this.failedOnce && next >= 55) {
        this.failedOnce = true
        const now = Date.now()
        this.setTask(t.id, {
          status: 'waiting', progress: next, stage: 'recovery queued', updatedAt: now,
          recovery: {
            jobId: `demo-${t.id}`, state: 'QUEUED', attempts: 0, maxAttempts: 2,
            nextAttemptAt: now + 30_000, enqueuedAt: now, queueOrder: this.logSeq,
            manualPriority: 0, canRetry: true, errorClass: 'EXECUTION',
            lastError: 'Contract test assertion failed: 3 cases (demo)',
          },
        })
        this.log('ERROR', `${t.id} ${t.name}: 3 contract tests failed (demo)`, t.id, t.executor)
        delete this.assignments[t.id]
        changed = true
        continue
      }
      if (next >= 100) {
        this.setTask(t.id, {
          status: 'completed', progress: 100, stage: 'Merged', updatedAt: Date.now(),
          recovery: t.recovery ? { ...t.recovery, state: 'RESOLVED', canRetry: false } : undefined,
        })
        this.log('SUCCESS', `${t.id} ${t.name}: tests passed, merged locally`, t.id, t.executor)
        delete this.assignments[t.id]
      } else {
        const stage = next < 35 ? 'Implementing' : next < 75 ? 'Testing' : 'Reviewing'
        this.setTask(t.id, { progress: next, stage, updatedAt: Date.now() })
      }
      changed = true
    }

    while (this.tasks.filter((t) => t.status === 'running').length < this.concurrency) {
      const done = new Set(this.tasks.filter((t) => t.status === 'completed').map((t) => t.id))
      const ready = this.tasks.find((t) => t.status === 'pending' && t.deps.every((d) => done.has(d)))
        ?? this.tasks.filter((t) => t.recovery?.state === 'QUEUED'
          && t.recovery.attempts < t.recovery.maxAttempts && t.recovery.nextAttemptAt <= Date.now()
          && t.deps.every((d) => done.has(d))).sort(compareRetryPriority)[0]
      if (ready) {
        this.assignWorker(ready)
        this.setTask(ready.id, {
          status: 'running', progress: ready.recovery ? ready.progress : 1,
          stage: ready.recovery ? 'recovery running' : 'Specification',
          worktree: `AetherTrade-${ready.id.toLowerCase()}`, updatedAt: Date.now(),
          recovery: ready.recovery ? {
            ...ready.recovery, state: 'CLAIMED', attempts: ready.recovery.attempts + 1,
            manualPriority: 0, canRetry: false, retryBlockedReason: '任务已领取，正在修复',
          } : undefined,
        })
        this.startedAt[ready.id] = Date.now()
        this.log('INFO', `${ready.executor} Worker: started ${ready.id} ${ready.name}`, ready.id, ready.executor)
        changed = true
      } else break
    }

    if (this.tasks.every((t) => t.status === 'completed')) {
      this.runState = 'stopped'
      this.log('SUCCESS', 'All demo tasks completed', undefined)
      changed = true
    }

    if (changed || this.runtimeSec % 5 === 0) this.emit()
  }

  async load(): Promise<Snapshot> {
    await this.readCatalogs()
    return this.snapshot
  }

  private executionSummary(task: Task): ExecutionSummary {
    const started = this.startedAt[task.id] ?? task.updatedAt
    const now = task.status === 'running' ? Date.now() : task.updatedAt
    const sequence = Math.max(1, Math.floor((now - started) / 2000))
    const messages = ['read · completed · src/contracts.py', 'edit · completed · tests/test_contracts.py', 'shell · running · python -m unittest']
    return {
      id: `demo-${task.id}-${started}`, stage: task.stage, startedAt: started,
      lastOutputAt: started + sequence * 2000, lastActivityAt: started + sequence * 2000,
      currentOperation: messages[sequence % messages.length],
      finishedAt: task.status === 'running' ? null : now, exitCode: task.status === 'running' ? null : 0,
      outputBytes: sequence * 160, sequence, telemetry: 'demo', pid: null, coordinatorPid: null,
      processState: task.status === 'running' ? 'alive' : 'finished',
    }
  }

  async loadExecution(taskId: string, after: number, attempt: string | null): Promise<ExecutionDetail> {
    const task = this.tasks.find(row => row.id === taskId)
    if (!task) throw new Error('未知模拟任务')
    const execution = this.executionSummary(task)
    if (attempt !== execution.id) after = 0
    const events = Array.from({ length: Math.min(8, execution.sequence) }, (_, index) => {
      const seq = execution.sequence - Math.min(8, execution.sequence) + index + 1
      return { seq, time: execution.startedAt! + seq * 2000, kind: 'tool',
        message: `${seq % 2 ? 'read' : 'edit'} · completed · src/contracts.py (demo)`,
        detail: '模拟工具事件，仅用于展示实时日志；没有执行真实命令。' }
    }).filter(row => row.seq > after)
    return { taskId, state: task.status, execution, events,
      cursor: execution.sequence, truncated: !!events.length && events[0].seq > after + 1 }
  }

  private async readCatalogs() {
    if (this.catalogPending) return
    this.catalogPending = true
    this.catalogReadAt = Date.now()
    try {
      const response = await fetch('/api/autodispatch/models', { signal: AbortSignal.timeout(5000) })
      if (!response.ok) throw new Error('官方模型目录不可用')
      this.catalogs = await response.json() as Record<Executor, ModelCatalog>
    } catch {
      const unavailable: ModelCatalog = { status: 'unavailable', source: '', models: [], error: '官方目录不可用，请启动本地接口后刷新' }
      this.catalogs = { Codex: unavailable, OpenCode: unavailable }
    } finally {
      this.catalogPending = false
      this.emit()
    }
  }

  subscribe(listener: (snapshot: Snapshot) => void) {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  async control(action: 'pause' | 'resume' | 'stop') {
    if (action === 'pause') this.runState = 'paused'
    if (action === 'resume') this.runState = 'running'
    if (action === 'stop') this.runState = 'stopped'
    this.log('WARN', `Demo scheduler: ${action} requested from console`)
    this.emit()
  }

  async retryTask(taskId: string) {
    const t = this.tasks.find((x) => x.id === taskId)
    if (!t?.recovery?.canRetry || t.recovery.state !== 'QUEUED') throw new Error('任务当前不可重试')
    this.retryPriority += 1
    this.setTask(taskId, {
      updatedAt: Date.now(),
      recovery: { ...t.recovery, manualPriority: this.retryPriority, nextAttemptAt: Date.now() },
    })
    this.log('INFO', `${taskId} 已置于重试队列最前 (demo)`, taskId)
    this.emit()
  }

  async configureWorker(slot: number, executor: Executor, profile: WorkerProfile) {
    const option = this.catalogs[executor].models.find(row => row.model === profile.model)
    const defaults = profile.model === null && profile.reasoningEffort === null
    if (slot < 1 || slot > this.concurrency || this.catalogs[executor].status !== 'ready'
      || (!defaults && !option?.reasoningEfforts.includes(profile.reasoningEffort))) throw new Error('官方目录未提供该 Worker 配置')
    this.workerPrefs[`${slot}:${executor}`] = { ...profile }
    this.log('INFO', `Worker ${slot} 配置已保存，下次领取任务生效 (demo)`)
    this.emit()
  }

  async refreshModels() {
    const res = await fetch('/api/autodispatch/models/refresh', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    })
    if (!res.ok) throw new Error('刷新官方模型目录失败')
    await this.readCatalogs()
  }

  dispose(): void {
    if (this.timer !== null) window.clearInterval(this.timer)
    this.listeners.clear()
  }
}

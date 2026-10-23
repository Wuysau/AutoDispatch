export type TaskStatus = 'completed' | 'running' | 'pending' | 'waiting' | 'failed'
export type Executor = 'Codex' | 'OpenCode'

export interface Task {
  id: string
  name: string
  status: TaskStatus
  executor: Executor
  stage: string
  deps: string[]
  progress: number | null
  updatedAt: number
  blockedBy?: string[]
  blockedReason?: string
  lastError?: string
  worktree?: string
  execution?: ExecutionSummary
  recovery?: {
    jobId: string
    state: 'QUEUED' | 'CLAIMED' | 'RESOLVED' | 'EXHAUSTED' | 'BLOCKED' | 'RELEASED'
    attempts: number
    maxAttempts: number
    nextAttemptAt: number
    enqueuedAt?: number
    queueOrder?: number
    manualPriority?: number
    canRetry?: boolean
    retryBlockedReason?: string | null
    errorClass: string
    lastError: string
  }
}

export interface ExecutionSummary {
  id: string | null
  stage: string
  startedAt: number | null
  lastOutputAt: number | null
  lastActivityAt: number | null
  currentOperation: string | null
  finishedAt: number | null
  exitCode: number | null
  outputBytes: number
  sequence: number
  telemetry: 'live' | 'unavailable' | 'demo'
  pid: number | null
  coordinatorPid: number | null
  processState: 'starting' | 'alive' | 'exited' | 'finished' | 'between_commands' | 'unknown'
}

export interface ExecutionEvent {
  seq: number
  time: number
  kind: string
  message: string
  detail: string
}

export interface ExecutionDetail {
  taskId: string
  state: string
  execution: ExecutionSummary
  events: ExecutionEvent[]
  cursor: number
  truncated: boolean
}

export interface WorkerProfile {
  model: string | null
  reasoningEffort: string | null
}

export interface WorkerModelOption {
  model: string | null
  reasoningEfforts: (string | null)[]
  displayName?: string
  defaultReasoningEffort?: string | null
}

export interface ModelCatalog {
  status: 'loading' | 'ready' | 'unavailable' | 'disabled'
  source: string
  models: WorkerModelOption[]
  updatedAt?: number
  error?: string
}

export interface Worker {
  slot: number
  executor: Executor
  taskId: string | null
  elapsedSec: number
  profile: WorkerProfile
  nextProfile: WorkerProfile
  modelOptions: WorkerModelOption[]
  configurable: boolean
  catalogStatus: ModelCatalog['status']
  catalogSource: string
  catalogUpdatedAt?: number
  catalogError?: string
  issue?: string
  defaultProfile?: WorkerProfile
}

export type LogLevel = 'INFO' | 'SUCCESS' | 'WARN' | 'ERROR'

export interface LogEntry {
  id: string
  time: number
  level: LogLevel
  source: string
  message: string
  taskId?: string
}

export type RunState = 'running' | 'paused' | 'stopped'

export interface Snapshot {
  source: 'demo' | 'live'
  project: string
  subtitle: string
  runState: RunState
  runtimeSec: number
  concurrency: number
  dagMaxParallel: number
  workerMode: 'auto' | 'limited'
  tasks: Task[]
  workers: Worker[]
  workerIssues: Worker[]
  logs: LogEntry[]
  updatedAt: number
}

export interface DataAdapter {
  name: string
  load(): Promise<Snapshot>
  subscribe(listener: (snapshot: Snapshot) => void): () => void
  setPollRate?(ms: number): void
  control(action: 'pause' | 'resume' | 'stop'): Promise<void>
  retryTask(taskId: string): Promise<void>
  configureWorker(slot: number, executor: Executor, profile: WorkerProfile): Promise<void>
  refreshModels(): Promise<void>
  loadExecution(taskId: string, after: number, attempt: string | null, signal?: AbortSignal): Promise<ExecutionDetail>
  dispose?(): void
}

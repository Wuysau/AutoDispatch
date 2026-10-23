import type { DataAdapter, ExecutionDetail, Executor, Snapshot, WorkerProfile } from '../types'
import { DemoEngine } from './demoData'

/**
 * 实时适配器：连接本机 AutoDispatch 后端（python -m autodispatch api）。
 *   GET  /api/autodispatch/snapshot              → Snapshot JSON
 *   POST /api/autodispatch/control  { action }   → action: stop（pause/resume 后端会返回 409）
 *   POST /api/autodispatch/tasks/:id/retry       → 置顶现有恢复任务；不可重试时返回 409
 * 接口不可用时自动回退到 Demo 数据，并显示 DEMO 标识。
 */
const API_BASE = '/api/autodispatch'
const POLL_MS = 2000
const POLL_MAX_BACKOFF_MS = 10000

async function failure(res: Response, fallback: string): Promise<Error> {
  try {
    const body = (await res.json()) as { error?: string }
    if (body.error) return new Error(body.error)
  } catch {
    // 非 JSON 响应时使用通用文案
  }
  return new Error(`${fallback}（HTTP ${res.status}）`)
}

export class LiveAdapter implements DataAdapter {
  name = 'AutoDispatch 实时'
  private listeners = new Set<(s: Snapshot) => void>()
  private timer: number | null = null
  private failures = 0
  private pollMs = POLL_MS
  private last: Snapshot | null = null
  private inFlight: Promise<Snapshot> | null = null

  private onVisible = () => {
    if (!document.hidden) this.schedule(0)
  }

  load(): Promise<Snapshot> {
    if (!this.inFlight) {
      this.inFlight = this.fetchSnapshot().finally(() => {
        this.inFlight = null
      })
    }
    return this.inFlight
  }

  setPollRate(ms: number) {
    this.pollMs = ms
    if (this.timer !== null && this.listeners.size > 0) this.schedule(ms)
  }

  private async fetchSnapshot(): Promise<Snapshot> {
    const res = await fetch(`${API_BASE}/snapshot`, { cache: 'no-cache' })
    if (res.status === 304 && this.last) return this.last
    if (!res.ok) throw await failure(res, '读取快照失败')
    const snapshot = (await res.json()) as Snapshot
    this.last = snapshot
    this.listeners.forEach((l) => l(snapshot))
    return snapshot
  }

  subscribe(listener: (snapshot: Snapshot) => void) {
    this.listeners.add(listener)
    if (this.timer === null) {
      document.addEventListener('visibilitychange', this.onVisible)
      this.schedule(this.pollMs)
    }
    return () => {
      this.listeners.delete(listener)
      if (this.listeners.size === 0) this.stopPolling()
    }
  }

  private schedule(delay: number) {
    if (this.timer !== null) window.clearTimeout(this.timer)
    this.timer = window.setTimeout(() => void this.tick(), delay)
  }

  private async tick() {
    this.timer = null
    if (document.hidden || this.listeners.size === 0) return
    try {
      await this.load()
      this.failures = 0
    } catch {
      this.failures += 1
    }
    if (this.listeners.size === 0) return
    this.schedule(Math.min(this.pollMs * 2 ** this.failures, Math.max(POLL_MAX_BACKOFF_MS, this.pollMs)))
  }

  private stopPolling() {
    if (this.timer !== null) window.clearTimeout(this.timer)
    this.timer = null
    document.removeEventListener('visibilitychange', this.onVisible)
  }

  dispose() {
    this.listeners.clear()
    this.stopPolling()
  }

  async control(action: 'pause' | 'resume' | 'stop') {
    const res = await fetch(`${API_BASE}/control`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action }),
    })
    if (!res.ok) throw await failure(res, '控制操作失败')
    await this.load().catch(() => undefined)
  }

  async retryTask(taskId: string) {
    const res = await fetch(`${API_BASE}/tasks/${encodeURIComponent(taskId)}/retry`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    })
    if (!res.ok) throw await failure(res, '重试请求失败')
    // A poll started before the mutation may still contain the old queue order.
    await this.inFlight?.catch(() => undefined)
    await this.load().catch(() => undefined)
  }

  async configureWorker(slot: number, executor: Executor, profile: WorkerProfile) {
    const res = await fetch(`${API_BASE}/workers/${slot}/settings`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ executor, ...profile }),
    })
    if (!res.ok) throw await failure(res, '保存 Worker 配置失败')
    await this.inFlight?.catch(() => undefined)
    await this.load().catch(() => undefined)
  }

  async refreshModels() {
    const res = await fetch(`${API_BASE}/models/refresh`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    })
    if (!res.ok) throw await failure(res, '刷新官方模型目录失败')
    await this.load().catch(() => undefined)
  }

  async loadExecution(taskId: string, after: number, attempt: string | null, signal?: AbortSignal): Promise<ExecutionDetail> {
    const query = new URLSearchParams({ after: String(after) })
    if (attempt) query.set('attempt', attempt)
    const res = await fetch(`${API_BASE}/tasks/${encodeURIComponent(taskId)}/execution?${query}`, {
      cache: 'no-store', signal,
    })
    if (!res.ok) throw await failure(res, '读取执行详情失败')
    return res.json() as Promise<ExecutionDetail>
  }
}

async function hasLiveBackend(): Promise<boolean> {
  try {
    const controller = new AbortController()
    const timeout = window.setTimeout(() => controller.abort(), 800)
    const res = await fetch(`${API_BASE}/snapshot`, { cache: 'no-store', signal: controller.signal })
    window.clearTimeout(timeout)
    return res.ok && (res.headers.get('content-type') ?? '').includes('application/json')
  } catch {
    return false
  }
}

export async function createAdapter(forceDemo: boolean, requireLive = false): Promise<DataAdapter> {
  if (!forceDemo && (requireLive || (await hasLiveBackend()))) return new LiveAdapter()
  return new DemoEngine()
}

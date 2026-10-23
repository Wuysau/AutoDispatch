import type { LogEntry, LogLevel, Task, TaskStatus } from './types'

export function newestLogs(logs: LogEntry[]): LogEntry[] {
  return [...logs].sort((a, b) => b.time - a.time || b.id.localeCompare(a.id, undefined, { numeric: true }))
}

export function compareFailureArrival(a: Task, b: Task): number {
  return (a.recovery?.enqueuedAt ?? a.updatedAt) - (b.recovery?.enqueuedAt ?? b.updatedAt)
    || (a.recovery?.queueOrder ?? 0) - (b.recovery?.queueOrder ?? 0)
    || a.id.localeCompare(b.id)
}

export function compareRetryPriority(a: Task, b: Task): number {
  return (b.recovery?.manualPriority ?? 0) - (a.recovery?.manualPriority ?? 0) || compareFailureArrival(a, b)
}

/** Maximum independent DAG frontier, including parallel tasks across graph layers. */
export function dagMaxParallel(tasks: Task[]): number {
  const byId = new Map(tasks.map(task => [task.id, task]))
  const ancestors = new Map<string, Set<string>>()
  const visit = (id: string): Set<string> => {
    const cached = ancestors.get(id)
    if (cached) return cached
    const reachable = new Set<string>()
    ancestors.set(id, reachable)
    for (const dep of byId.get(id)?.deps ?? []) {
      if (!byId.has(dep)) continue
      reachable.add(dep)
      visit(dep).forEach(ancestor => reachable.add(ancestor))
    }
    return reachable
  }
  tasks.forEach(task => visit(task.id))
  const matched = new Map<string, string>()
  const augment = (id: string, seen: Set<string>): boolean => {
    for (const dep of ancestors.get(id) ?? []) {
      if (seen.has(dep)) continue
      seen.add(dep)
      if (!matched.has(dep) || augment(matched.get(dep)!, seen)) { matched.set(dep, id); return true }
    }
    return false
  }
  return tasks.length - tasks.filter(task => augment(task.id, new Set())).length
}

export const STATUS_META: Record<TaskStatus, { label: string; tone: string; order: number }> = {
  running: { label: '运行中', tone: 'blue', order: 0 },
  failed: { label: '失败', tone: 'red', order: 1 },
  pending: { label: '待执行', tone: 'gray', order: 2 },
  waiting: { label: '等待依赖', tone: 'amber', order: 3 },
  completed: { label: '已完成', tone: 'green', order: 4 },
}

export const LEVEL_TONE: Record<LogLevel, string> = {
  INFO: 'blue',
  SUCCESS: 'green',
  WARN: 'amber',
  ERROR: 'red',
}

export function formatDuration(totalSec: number): string {
  const s = Math.max(0, Math.floor(totalSec))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(h)}:${pad(m)}:${pad(sec)}`
}

export function formatClock(ts: number): string {
  const d = new Date(ts)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

export function countBy(tasks: Task[]) {
  const counts: Record<TaskStatus, number> = { running: 0, pending: 0, waiting: 0, completed: 0, failed: 0 }
  tasks.forEach((t) => {
    counts[t.status] += 1
  })
  return counts
}

export function percent(done: number, total: number): number {
  return total === 0 ? 0 : Math.round((done / total) * 1000) / 10
}

/**
 * 概览页使用的执行前沿子图：运行中/失败/可立即执行的任务，加上其上游一跳与下游两跳。
 * 完整依赖图仍在 DAG 页面展示。
 */
export function focusTasks(tasks: Task[], downstreamDepth = 2): Task[] {
  const byId = new Map(tasks.map((t) => [t.id, t]))
  const children = new Map<string, string[]>()
  tasks.forEach((t) =>
    t.deps.forEach((d) => children.set(d, [...(children.get(d) ?? []), t.id])),
  )
  const isFrontier = (t: Task) =>
    t.status === 'running' ||
    t.status === 'failed' ||
    (t.status === 'pending' && t.deps.every((d) => byId.get(d)?.status === 'completed'))
  const keep = new Set(tasks.filter(isFrontier).map((t) => t.id))
  if (keep.size === 0) return tasks

  const frontier = [...keep]
  frontier.forEach((id) => {
    byId.get(id)?.deps.forEach((d) => {
      if (byId.has(d)) keep.add(d)
    })
  })
  let layer = frontier
  for (let depth = 0; depth < downstreamDepth; depth += 1) {
    layer = layer.flatMap((id) => children.get(id) ?? [])
    layer.forEach((id) => keep.add(id))
  }
  return tasks.filter((t) => keep.has(t.id))
}

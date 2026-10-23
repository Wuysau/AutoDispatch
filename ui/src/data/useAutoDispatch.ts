import { useCallback, useEffect, useState } from 'react'
import type { DataAdapter, Snapshot } from '../types'
import { createAdapter } from './adapter'

export interface AutoDispatchState {
  snapshot: Snapshot | null
  adapter: DataAdapter | null
  loading: boolean
  error: string | null
  reload: () => void
}

function keepUnchanged(prev: Snapshot | null, next: Snapshot): Snapshot {
  if (!prev) return next
  const same = <T,>(a: T, b: T) => JSON.stringify(a) === JSON.stringify(b)
  return {
    ...next,
    tasks: same(prev.tasks, next.tasks) ? prev.tasks : next.tasks,
    workers: same(prev.workers, next.workers) ? prev.workers : next.workers,
    logs: same(prev.logs, next.logs) ? prev.logs : next.logs,
  }
}

export function useAutoDispatch(): AutoDispatchState {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const [adapter, setAdapter] = useState<DataAdapter | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let cancelled = false
    let unsubscribe: (() => void) | undefined
    let adapterRef: DataAdapter | undefined
    setLoading(true)
    setError(null)
    const query = new URLSearchParams(window.location.search)
    const forceDemo = query.get('mode') === 'demo'
    const requireLive = query.get('mode') === 'live' || query.get('host') === 'desktop'
    createAdapter(forceDemo, requireLive)
      .then(async (a) => {
        adapterRef = a
        if (cancelled) {
          a.dispose?.()
          return
        }
        setAdapter(a)
        const first = await a.load()
        if (cancelled) return
        setSnapshot(first)
        setLoading(false)
        unsubscribe = a.subscribe((s) => {
          if (!cancelled) setSnapshot((prev) => keepUnchanged(prev, s))
        })
      })
      .catch((e: unknown) => {
        if (cancelled) return
        setError(e instanceof Error ? e.message : String(e))
        setLoading(false)
      })
    return () => {
      cancelled = true
      unsubscribe?.()
      adapterRef?.dispose?.()
    }
  }, [attempt])

  const reload = useCallback(() => setAttempt((n) => n + 1), [])
  return { snapshot, adapter, loading, error, reload }
}

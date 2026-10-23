import { createContext, useContext } from 'react'
import type { DataAdapter, Snapshot, TaskStatus } from '../types'
import type { Prefs } from '../data/prefs'

export type TabId = 'overview' | 'tasks' | 'dag' | 'logs' | 'settings'

export interface Toast {
  id: number
  message: string
  tone: 'info' | 'success' | 'warn' | 'error'
}

export interface AppContextValue {
  snapshot: Snapshot
  adapter: DataAdapter
  prefs: Prefs
  updatePrefs: (patch: Partial<Prefs>) => void
  selectedTaskId: string | null
  selectTask: (id: string | null) => void
  tab: TabId
  statusFilter: TaskStatus | 'all'
  setStatusFilter: (f: TaskStatus | 'all') => void
  setTab: (tab: TabId) => void
  notify: (message: string, tone?: Toast['tone']) => void
  runAction: (fn: () => Promise<void>, okMessage?: string) => Promise<void>
  reloadData: () => void
}

export const AppContext = createContext<AppContextValue | null>(null)

export function useApp(): AppContextValue {
  const ctx = useContext(AppContext)
  if (!ctx) throw new Error('useApp must be used within AppContext')
  return ctx
}

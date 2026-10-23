import { useEffect, useState } from 'react'

export interface Prefs {
  opacity: number
  blur: number
  widgetVisible: boolean
  widgetCollapsed: boolean
}

export const DEFAULT_PREFS: Prefs = {
  opacity: 0.72,
  blur: 20,
  widgetVisible: true,
  widgetCollapsed: false,
}

const KEY = 'autodispatch.ui.prefs.v1'

function load(): Prefs {
  try {
    const raw = window.localStorage.getItem(KEY)
    if (!raw) return DEFAULT_PREFS
    const prefs = { ...DEFAULT_PREFS, ...(JSON.parse(raw) as Partial<Prefs>) }
    return prefs
  } catch {
    return DEFAULT_PREFS
  }
}

export function usePrefs() {
  const [prefs, setPrefs] = useState<Prefs>(load)

  useEffect(() => {
    try {
      window.localStorage.setItem(KEY, JSON.stringify(prefs))
    } catch {
      /* 存储不可用时仅保持内存状态 */
    }
    document.documentElement.style.setProperty('--glass-alpha', String(prefs.opacity))
    document.documentElement.style.setProperty('--glass-blur', `${prefs.blur}px`)
  }, [prefs])

  useEffect(() => {
    const sync = (event: StorageEvent) => { if (event.key === KEY) setPrefs(load()) }
    window.addEventListener('storage', sync)
    return () => window.removeEventListener('storage', sync)
  }, [])

  const update = (patch: Partial<Prefs>) => setPrefs((p) => ({ ...p, ...patch }))
  const reset = () => setPrefs(DEFAULT_PREFS)
  return { prefs, update, reset }
}

import { useEffect, useState } from 'react'
const API_BASE = '/api/autodispatch'
export type HostTarget = 'widget' | 'console'
export const nativeWindow = new URLSearchParams(window.location.search).get('host') === 'desktop'
function currentTarget(): HostTarget { return window.location.hash === '#/console' ? 'console' : 'widget' }
let hostPresent = false
export interface NativeWindowState {
  visible: boolean
  pinned: boolean
  material?: { mode: 'acrylic' | 'fallback'; reason?: string }
}
function syncMaterial(data: { windows?: Partial<Record<HostTarget, NativeWindowState>> }) {
  document.documentElement.dataset.desktopMaterial = data.windows?.[currentTarget()]?.material?.mode ?? 'fallback'
}
export async function detectHost(): Promise<boolean> {
  if (!nativeWindow) return false
  try {
    const res = await fetch(`${API_BASE}/ui/host`, { cache: 'no-store' })
    const data = res.ok ? await res.json() : null
    hostPresent = Boolean(data?.host)
    if (data) syncMaterial(data)
  } catch { hostPresent = false }
  return hostPresent
}
export function hostIsPresent() { return nativeWindow && hostPresent }
export async function requestHostAction(action: string, target: HostTarget = currentTarget(), extra: Record<string, unknown> = {}): Promise<void> {
  if (!nativeWindow || !await detectHost()) throw new Error('原生窗口宿主未连接，请从桌面启动器打开')
  const res = await fetch(`${API_BASE}/ui/host`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action, target, ...extra }) })
  const data = await res.json()
  if (!res.ok) throw new Error(data.error || '窗口请求失败')
  const deadline = Date.now() + 4500
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 150))
    const response = await fetch(`${API_BASE}/ui/host`, { cache: 'no-store' })
    if (!response.ok) throw new Error('无法读取窗口回执')
    const state = await response.json()
    const receipt = state.results?.[String(data.seq)]
    if (receipt) {
      if (!receipt.ok) throw new Error(receipt.error || '原生窗口操作失败')
      return
    }
  }
  throw new Error('窗口操作超时，未收到宿主确认')
}
let pendingSize: { width: number; height: number } | null = null
let sizeTimer: number | null = null
let resizingWidget = false
let resizeInFlight = false
type NativeResizeWindow = Window & { pywebview?: { api?: {
  resize_widget?: (width: number, height: number) => Promise<unknown>
  begin_drag?: () => Promise<unknown>
} } }
export function beginNativeDrag(): boolean {
  const bridge = (window as NativeResizeWindow).pywebview?.api?.begin_drag
  if (!nativeWindow || !bridge) return false
  void bridge().catch((error) => console.warn('Native drag failed', error))
  return true
}
async function resizeDirect(width: number, height: number) {
  const bridge = (window as NativeResizeWindow).pywebview?.api?.resize_widget
  if (bridge) await bridge(width, height)
  else await requestHostAction('size', 'widget', { width, height })
}

export async function resizeWidgetAndCommit(width: number, height: number, commit: () => void) {
  resizingWidget = true
  pendingSize = null
  if (sizeTimer !== null) window.cancelAnimationFrame(sizeTimer)
  sizeTimer = null
  try {
    // Finish an earlier automatic fit before committing the user's new size.
    while (resizeInFlight) await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()))
    await resizeDirect(Math.round(width), Math.ceil(height))
    commit()
  } finally { resizingWidget = false }
}

export function requestHostSize(width: number, height: number) {
  if (!nativeWindow || resizingWidget) return
  pendingSize = { width: Math.round(width), height: Math.round(height) }
  if (sizeTimer !== null) return
  sizeTimer = window.requestAnimationFrame(async () => {
    sizeTimer = null
    const size = pendingSize
    pendingSize = null
    if (!size || resizingWidget) return
    if (resizeInFlight) { requestHostSize(size.width, size.height); return }
    resizeInFlight = true
    try { await resizeDirect(size.width, size.height) }
    catch (e) { console.warn(e) }
    finally { resizeInFlight = false }
  })
}
export function requestHostPin(value: boolean) { return requestHostAction('pin', currentTarget(), { value }) }
export function requestHostConsole(taskId?: string) { return requestHostAction('console', 'widget', taskId ? { task_id: taskId } : {}) }

export function useNativeWindowState(target: HostTarget) {
  const [state, setState] = useState<NativeWindowState | null>(null)
  useEffect(() => {
    if (!nativeWindow) return
    let disposed = false
    const refresh = async () => {
      try {
        const res = await fetch(`${API_BASE}/ui/host`, { cache: 'no-store' })
        if (!res.ok) return
        const data = await res.json()
        if (!disposed) {
          syncMaterial(data)
          setState(data.host ? data.windows?.[target] || null : null)
        }
      } catch { if (!disposed) setState(null) }
    }
    void refresh()
    const timer = window.setInterval(() => void refresh(), 1000)
    return () => { disposed = true; window.clearInterval(timer) }
  }, [target])
  return state
}

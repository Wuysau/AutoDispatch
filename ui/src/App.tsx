import { useCallback, useEffect, useState } from 'react'
import { AlertCircle, Box, CheckCircle2, Info, TriangleAlert, X } from 'lucide-react'
import { AppContext, type AppContextValue, type TabId, type Toast } from './components/AppContext'
import { Console } from './components/Console'
import { FloatingWidget } from './components/FloatingWidget'
import { ErrorState, LoadingState } from './components/ui'
import { useAutoDispatch } from './data/useAutoDispatch'
import { usePrefs } from './data/prefs'
import { useNativeDrag } from './data/useNativeDrag'
import { detectHost, nativeWindow, requestHostAction, requestHostConsole, requestHostSize } from './data/hostBridge'
import type { TaskStatus } from './types'

const TOAST_ICON = { info: Info, success: CheckCircle2, warn: TriangleAlert, error: AlertCircle }
export function App() {
  const consoleWindow = window.location.hash === '#/console'
  const preview = !nativeWindow && window.location.hash === '#/preview'
  const [consoleVisible, setConsoleVisible] = useState(true)
  const { snapshot, adapter, loading, error, reload } = useAutoDispatch()
  const { prefs, update } = usePrefs()
  const [hostReady, setHostReady] = useState(false)
  const [tab, setTab] = useState<TabId>('overview')
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(new URLSearchParams(window.location.search).get('task'))
  const [statusFilter, setStatusFilter] = useState<TaskStatus | 'all'>('all')
  const [toasts, setToasts] = useState<Toast[]>([])
  const notify = useCallback((message: string, tone: Toast['tone'] = 'info') => {
    const id = Date.now() + Math.random()
    setToasts((list) => [...list.slice(-3), { id, message, tone }])
    window.setTimeout(() => setToasts((list) => list.filter((t) => t.id !== id)), 4000)
  }, [])
  const runAction = useCallback(async (fn: () => Promise<void>, okMessage?: string) => {
    try { await fn(); if (okMessage) notify(okMessage, 'success') }
    catch (e) { notify(e instanceof Error ? e.message : String(e), 'error') }
  }, [notify])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setSelectedTaskId(null) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  useEffect(() => {
    document.title = snapshot ? `AutoDispatch · ${snapshot.tasks.filter((t) => t.status === 'completed').length}/${snapshot.tasks.length}` : 'AutoDispatch'
  }, [snapshot])
  useEffect(() => {
    document.documentElement.classList.toggle('native-surface', nativeWindow)
    document.documentElement.classList.toggle('console-surface', consoleWindow)
    let attempts = 0
    const check = () => { void detectHost().then(setHostReady); if (++attempts >= 20) window.clearInterval(timer) }
    const timer = window.setInterval(check, 500)
    check()
    return () => window.clearInterval(timer)
  }, [consoleWindow])
  useEffect(() => {
    if (!consoleWindow) adapter?.setPollRate?.(prefs.widgetVisible ? 2000 : 10000)
  }, [adapter, consoleWindow, prefs.widgetVisible])

  const contextValue: AppContextValue | null = snapshot && adapter ? {
    snapshot, adapter, prefs, updatePrefs: update, selectedTaskId, selectTask: setSelectedTaskId,
    tab, setTab, statusFilter, setStatusFilter, notify, runAction, reloadData: reload,
  } : null
  const hasContent = Boolean(contextValue)
  useNativeDrag(hasContent)
  useEffect(() => {
    if (consoleWindow || !hostReady) return
    const widget = document.querySelector<HTMLElement>('.widget')
    const fit = () => { if (widget) requestHostSize(440, Math.ceil(widget.getBoundingClientRect().height)) }
    fit()
    const observer = new ResizeObserver(fit)
    if (widget) observer.observe(widget)
    return () => observer.disconnect()
  }, [consoleWindow, hasContent, hostReady, prefs.widgetCollapsed])

  const openDetails = (taskId?: string) => {
    if (preview) {
      setConsoleVisible(true)
      setSelectedTaskId(taskId ?? null)
      if (taskId) setTab('overview')
    } else if (nativeWindow) void runAction(() => requestHostConsole(taskId))
    else {
      const params = new URLSearchParams(window.location.search)
      params.delete('task')
      if (taskId) params.set('task', taskId)
      const popup = window.open(`${window.location.origin}/?${params}#/console`, 'autodispatch-console', 'popup,width=1040,height=940')
      if (popup) popup.focus()
      else notify('请允许弹出窗口以打开独立详情', 'warn')
    }
  }
  const hideWidget = () => {
    if (nativeWindow) void runAction(() => requestHostAction('hide'), '已隐藏，可从系统托盘恢复')
    else update({ widgetVisible: false })
  }
  return (
    <div className={`stage ${preview ? 'stage-preview' : consoleWindow ? 'stage-console-window' : 'stage-standalone'}`}>
      {!nativeWindow && <div className="backdrop" aria-hidden="true" />}
      {loading && <LoadingState />}
      {error && <ErrorState message={error} onRetry={reload} />}
      {contextValue && <AppContext.Provider value={contextValue}>
        {preview ? <div className="preview-layout">
          <div className="preview-widget">
            {prefs.widgetVisible ? <FloatingWidget onOpenDetails={openDetails} onClose={hideWidget} /> :
              <button className="btn" onClick={() => update({ widgetVisible: true })}><Box size={16} /> 显示悬浮窗</button>}
          </div>
          <div className="preview-console">
            {consoleVisible ? <Console onClose={() => setConsoleVisible(false)} /> :
              <button className="btn" onClick={() => setConsoleVisible(true)}>打开控制台</button>}
          </div>
        </div> : consoleWindow ? <div className="console-window"><Console onClose={() => {
          if (nativeWindow) void runAction(() => requestHostAction('hide'))
          else window.close()
        }} /></div> : prefs.widgetVisible || nativeWindow ?
          <FloatingWidget variant="standalone" onOpenDetails={openDetails} onClose={hideWidget} /> :
          <button className="widget-tab" title="恢复悬浮窗" aria-label="恢复悬浮窗" onClick={() => update({ widgetVisible: true })}><Box size={20} /></button>}
        {!nativeWindow && snapshot?.source === 'demo' && <div className="demo-banner"><span className="dot tone-dot-amber" />DEMO · 模拟数据，不代表真实调度结果</div>}
      </AppContext.Provider>}
      <div className="toast-layer" aria-live="polite">{toasts.map((t) => {
        const Icon = TOAST_ICON[t.tone]
        return <div key={t.id} className={`toast tone-${t.tone === 'info' ? 'blue' : t.tone === 'success' ? 'green' : t.tone === 'warn' ? 'amber' : 'red'}`}>
          <Icon size={14} /><span>{t.message}</span>
          <button className="icon-btn icon-btn-sm" aria-label="关闭提示" onClick={() => setToasts((l) => l.filter((x) => x.id !== t.id))}><X size={12} /></button>
        </div>
      })}</div>
    </div>
  )
}

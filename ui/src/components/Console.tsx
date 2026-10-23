import { lazy, Suspense, useState, type ReactNode } from 'react'
import { Box, Minus, Pin, ScrollText, Settings2, Workflow, X } from 'lucide-react'
import { useApp, type TabId } from './AppContext'
import { DemoTag, Glass, LoadingState } from './ui'
import { OverviewView } from './OverviewView'
import { TasksView } from './TasksView'

const DagView = lazy(() => import('./DagView').then((m) => ({ default: m.DagView })))
import { LogsView } from './LogsView'
import { SettingsView } from './SettingsView'
import { TaskDetail } from './TaskDetail'
import { hostIsPresent, nativeWindow, requestHostAction, requestHostPin, useNativeWindowState } from '../data/hostBridge'

const TABS: { id: TabId; label: string; icon?: ReactNode }[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'tasks', label: 'Task List' },
  { id: 'dag', label: 'DAG', icon: <Workflow size={14} /> },
  { id: 'logs', label: 'Logs', icon: <ScrollText size={14} /> },
  { id: 'settings', label: 'Settings', icon: <Settings2 size={14} /> },
]

interface Props {
  onClose: () => void
}

export function Console({ onClose }: Props) {
  const { snapshot, tab, setTab, selectedTaskId, selectTask, notify, runAction } = useApp()
  const [minimized, setMinimized] = useState(false)
  const nativeState = useNativeWindowState('console')
  const pinned = nativeState?.pinned ?? false
  const selected = snapshot.tasks.find((t) => t.id === selectedTaskId) ?? null
  const showDrawer = !!selected && tab !== 'dag'

  const minimize = () => {
    if (nativeWindow) void runAction(() => requestHostAction('minimize'))
    else setMinimized((v) => !v)
  }

  const togglePin = () => {
    if (hostIsPresent()) {
      void runAction(async () => { await requestHostPin(!pinned) }, pinned ? '已取消置顶' : '控制台窗口已置顶')
    } else {
      notify('原生置顶需要 Windows 宿主支持', 'warn')
    }
  }

  return (
    <Glass className={`console ${minimized ? 'is-minimized' : ''} ${showDrawer ? 'has-drawer' : ''}`}>
      <header className="console-header">
        <div className="console-title pywebview-drag-region">
          <span className="brand-mark brand-lg">
            <Box size={18} />
          </span>
          <div>
            <div className="console-name">
              <strong>AutoDispatch</strong>
              {snapshot.source === 'demo' && <DemoTag />}
            </div>
            <div className="muted small">
              {snapshot.project} · {snapshot.subtitle}
            </div>
          </div>
        </div>
        <div className="window-controls">
          <button className="icon-btn icon-btn-window" title="最小化" aria-label="最小化" onClick={minimize}>
            <Minus size={15} />
          </button>
          <button className={`icon-btn icon-btn-window ${pinned ? 'is-active' : ''}`} title={pinned ? '取消置顶' : '窗口置顶'} aria-label="置顶" onClick={togglePin}>
            <Pin size={14} />
          </button>
          <button className="icon-btn icon-btn-window" title="关闭控制台" aria-label="关闭控制台" onClick={onClose}>
            <X size={15} />
          </button>
        </div>
      </header>

      {!minimized && (
        <>
          <nav className="tabs" role="tablist">
            {TABS.map((t) => (
              <button
                key={t.id}
                role="tab"
                aria-selected={tab === t.id}
                className={`tab ${tab === t.id ? 'is-active' : ''}`}
                onClick={() => setTab(t.id)}
              >
                {t.icon}
                {t.label}
              </button>
            ))}
          </nav>

          <div className={`console-body tab-${tab}`}>
            {tab === 'overview' && <OverviewView />}
            {tab === 'tasks' && <TasksView />}
            {tab === 'dag' && (
              <Suspense fallback={<LoadingState text="正在加载 DAG 模块…" />}>
                <DagView />
              </Suspense>
            )}
            {tab === 'logs' && <LogsView />}
            {tab === 'settings' && <SettingsView />}
          </div>

          {showDrawer && selected && (
            <aside className="drawer">
              <TaskDetail task={selected} onClose={() => selectTask(null)} />
            </aside>
          )}
        </>
      )}
    </Glass>
  )
}

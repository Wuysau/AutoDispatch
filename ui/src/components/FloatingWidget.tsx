import { ArrowRight, Box, ChevronRight, ChevronUp, ChevronDown, Pin, PinOff, X } from 'lucide-react'
import { useState } from 'react'
import { flushSync } from 'react-dom'
import { useApp } from './AppContext'
import { Glass, ProgressBar, Ring, DemoTag } from './ui'
import { countBy, percent } from '../utils'
import { hostIsPresent, nativeWindow, requestHostPin, resizeWidgetAndCommit, useNativeWindowState } from '../data/hostBridge'
import type { RunState } from '../types'

const RUN_LABEL: Record<RunState, { text: string; tone: string }> = {
  running: { text: 'Running', tone: 'green' },
  paused: { text: 'Paused', tone: 'amber' },
  stopped: { text: 'Stopped', tone: 'gray' },
}

interface Props {
  onOpenDetails: (taskId?: string) => void
  onClose?: () => void
  variant?: 'desktop' | 'standalone'
}

export function FloatingWidget({ onOpenDetails, onClose, variant = 'desktop' }: Props) {
  const { snapshot, prefs, updatePrefs, selectTask, notify, runAction } = useApp()
  const nativeState = useNativeWindowState('widget')
  const pinned = nativeState?.pinned ?? false
  const collapsed = prefs.widgetCollapsed
  const [resizing, setResizing] = useState(false)
  const counts = countBy(snapshot.tasks)
  const total = snapshot.tasks.length
  const done = counts.completed
  const progress = percent(done, total)
  const run = RUN_LABEL[snapshot.runState]
  const activeTasks = snapshot.tasks.filter((t) => t.status === 'running')

  const toggleCollapsed = async () => {
    if (resizing) return
    const next = !collapsed
    if (!nativeWindow) { updatePrefs({ widgetCollapsed: next }); return }
    const widget = document.querySelector<HTMLElement>('.widget')
    const compact = widget?.querySelector<HTMLElement>('.widget-compact')
    const expanded = widget?.querySelector<HTMLElement>('.widget-expanded')
    if (!widget || !compact || !expanded) return
    // Measure the target state without painting it, then resize the native
    // surface before React removes content. No empty acrylic tail is exposed.
    widget.classList.toggle('is-collapsed', next)
    compact.hidden = !next
    expanded.hidden = next
    const target = widget.getBoundingClientRect()
    widget.classList.toggle('is-collapsed', collapsed)
    compact.hidden = !collapsed
    expanded.hidden = collapsed
    setResizing(true)
    try {
      await resizeWidgetAndCommit(target.width, target.height,
        () => flushSync(() => updatePrefs({ widgetCollapsed: next })))
    } catch (error) { notify(error instanceof Error ? error.message : String(error), 'error') }
    finally { setResizing(false) }
  }

  const togglePin = () => {
    const next = !pinned
    if (hostIsPresent()) {
      void runAction(async () => { await requestHostPin(next) }, next ? '已置顶悬浮组件' : '已取消置顶')
    } else {
      notify('原生置顶需要 Windows 宿主支持，浏览器预览中仅作视觉状态', 'warn')
    }
  }

  return (
    <Glass className={`widget ${collapsed ? 'is-collapsed' : ''} ${variant === 'standalone' ? 'is-standalone' : ''}`}>
      <div className="widget-header">
        <div className="widget-brand pywebview-drag-region">
          <span className="brand-mark">
            <Box size={15} />
          </span>
          <strong>AutoDispatch</strong>
          <span className={`run-pill tone-${run.tone}`}>
            <span className="dot" />
            {run.text}
          </span>
          {snapshot.source === 'demo' && <DemoTag />}
        </div>
        <div className="widget-actions">
          <button
            className="icon-btn"
            title={collapsed ? '展开' : '折叠'}
            aria-label={collapsed ? '展开' : '折叠'}
            disabled={resizing}
            onClick={() => void toggleCollapsed()}
          >
            {collapsed ? <ChevronDown size={15} /> : <ChevronUp size={15} />}
          </button>
          <button
            className={`icon-btn ${pinned ? 'is-active' : ''}`}
            title={pinned ? '取消置顶' : '置顶（需原生宿主）'}
            aria-label="置顶"
            onClick={togglePin}
          >
            {pinned ? <PinOff size={14} /> : <Pin size={14} />}
          </button>
          {onClose && (
            <button className="icon-btn" title="隐藏到系统托盘" aria-label="隐藏到系统托盘" onClick={onClose}>
              <X size={15} />
            </button>
          )}
        </div>
      </div>

      <div className="widget-progress">
        <div className="row-between">
          <span>
            <strong className="num">{done}</strong>
            <span className="muted"> / {total} tasks</span>
          </span>
          <span className="num muted">{progress}%</span>
        </div>
        <ProgressBar value={progress} tone="green" />
      </div>

        <div className="widget-compact" hidden={!collapsed}>
          <span>
            <i className="dot tone-dot-blue" /> 运行 {counts.running}
          </span>
          <span>
            <i className="dot tone-dot-gray" /> 待执行 {counts.pending}
          </span>
          <span>
            <i className="dot tone-dot-amber" /> 等待 {counts.waiting}
          </span>
          {counts.failed > 0 && (
            <span className="text-red">
              <i className="dot tone-dot-red" /> 失败 {counts.failed}
            </span>
          )}
          <button className="link-btn" onClick={() => onOpenDetails()}>
            详情 <ChevronRight size={13} />
          </button>
        </div>
        <div className="widget-expanded" hidden={collapsed}>
          <div className="widget-chips">
            <Chip label="Running" value={counts.running} tone="green" />
            <Chip label="Pending" value={counts.pending} tone="blue" />
            <Chip label="Waiting" value={counts.waiting} tone="gray" />
          </div>

          <div className="widget-tasks">
            {activeTasks.length === 0 && <div className="muted small">当前没有执行中的任务</div>}
            {activeTasks.slice(0, 3).map((t) => (
              <button
                key={t.id}
                className="widget-task"
                onClick={() => {
                  selectTask(t.id)
                  onOpenDetails(t.id)
                }}
              >
                <Ring value={t.progress} tone="blue" size={28} />
                <div className="widget-task-text">
                  <div className="ellipsis">
                    <span className="task-id widget-task-id">{t.id}</span> <span className="task-name">{t.name}</span>
                  </div>
                  <div className="small">
                    <span className="text-blue">{t.executor}</span>
                    <span className="muted"> · {t.stage}</span>
                  </div>
                </div>
                <ChevronRight size={15} className="muted" />
              </button>
            ))}
          </div>

          <button className="btn btn-block widget-details" onClick={() => onOpenDetails()}>
            View Details <ArrowRight size={18} />
          </button>
        </div>
    </Glass>
  )
}

function Chip({ label, value, tone }: { label: string; value: number; tone: string }) {
  return (
    <div className="chip">
      <span className={`chip-ring tone-${tone}`}>{value}</span>
      <span className="chip-label">{label}</span>
    </div>
  )
}

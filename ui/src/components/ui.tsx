import type { ReactNode } from 'react'
import { AlertTriangle, Inbox, Loader2 } from 'lucide-react'
import type { TaskStatus } from '../types'
import { STATUS_META } from '../utils'

export function Glass({
  children,
  className = '',
  ...rest
}: { children: ReactNode; className?: string } & React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={`glass ${className}`} {...rest}>
      {children}
    </div>
  )
}

export function StatusBadge({ status, compact = false }: { status: TaskStatus; compact?: boolean }) {
  const meta = STATUS_META[status]
  return (
    <span className={`badge tone-${meta.tone} ${compact ? 'badge-compact' : ''}`}>
      <span className="dot" />
      {meta.label}
    </span>
  )
}

export function ProgressBar({ value, tone = 'blue' }: { value: number | null; tone?: string }) {
  if (value === null) return (
    <div className="progress progress-indeterminate" role="progressbar" aria-label="执行进度未知" aria-valuetext="执行进度未知">
      <div className={`progress-fill tone-bg-${tone}`} />
    </div>
  )
  const v = Math.max(0, Math.min(100, value))
  return (
    <div className="progress" role="progressbar" aria-valuenow={Math.round(v)} aria-valuemin={0} aria-valuemax={100}>
      <div className={`progress-fill tone-bg-${tone}`} style={{ transform: `scaleX(${v / 100})` }} />
    </div>
  )
}

export function Ring({ value, tone = 'blue', size = 22 }: { value: number | null; tone?: string; size?: number }) {
  const r = 8
  const c = 2 * Math.PI * r
  const v = value === null ? 25 : Math.max(0, Math.min(100, value))
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" className={`ring tone-${tone} ${value === null ? 'spin' : ''}`} aria-hidden>
      <circle cx="10" cy="10" r={r} className="ring-track" />
      <circle
        cx="10"
        cy="10"
        r={r}
        className="ring-value"
        strokeDasharray={c}
        strokeDashoffset={c - (v / 100) * c}
        transform="rotate(-90 10 10)"
      />
    </svg>
  )
}

export function DemoTag() {
  return <span className="demo-tag" title="当前显示的是模拟数据，不代表真实调度结果">DEMO</span>
}

export function LoadingState({ text = '正在载入调度数据…' }: { text?: string }) {
  return (
    <div className="state-box">
      <Loader2 className="spin" size={20} />
      <span>{text}</span>
    </div>
  )
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="state-box">
      <Inbox size={22} />
      <strong>{title}</strong>
      {hint && <span className="muted">{hint}</span>}
    </div>
  )
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="state-box state-error">
      <AlertTriangle size={22} />
      <strong>加载失败</strong>
      <span className="muted">{message}</span>
      {onRetry && (
        <button className="btn" onClick={onRetry}>
          重新加载
        </button>
      )}
    </div>
  )
}

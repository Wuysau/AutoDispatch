import { useRef, useState } from 'react'
import { RotateCcw } from 'lucide-react'
import type { Task } from '../types'
import { useApp } from './AppContext'

export function RetryButton({ task }: { task: Task }) {
  const { adapter, runAction } = useApp()
  const [pending, setPending] = useState(false)
  const inFlight = useRef(false)
  if (task.status !== 'failed' && (!task.recovery || task.recovery.state === 'RESOLVED')) return null
  const reason = task.recovery?.retryBlockedReason
    ?? (task.recovery?.canRetry ? undefined : '任务当前不可重试，请查看失败详情')
  return (
    <button
      className="btn btn-sm"
      disabled={pending || !task.recovery?.canRetry}
      aria-label={`重试 ${task.id}`}
      aria-busy={pending}
      title={reason ?? '移到重试队列最前，等待空闲 worker'}
      onKeyDown={(event) => event.stopPropagation()}
      onClick={async (event) => {
        event.stopPropagation()
        if (inFlight.current) return
        inFlight.current = true
        setPending(true)
        try {
          await runAction(() => adapter.retryTask(task.id), `${task.id} 已置于重试队列最前，等待调度`)
        } finally {
          inFlight.current = false
          setPending(false)
        }
      }}
    >
      <RotateCcw size={13} /> {pending ? '提交中…' : '重试'}
    </button>
  )
}

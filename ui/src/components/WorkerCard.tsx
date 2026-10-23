import { useEffect, useState } from 'react'
import { Clock, Laptop, Terminal } from 'lucide-react'
import type { Worker, WorkerProfile } from '../types'
import { formatDuration } from '../utils'
import { useApp } from './AppContext'
import { ProgressBar } from './ui'
import { processLabel, activityAge } from './ExecutionPanel'

const same = (a: WorkerProfile, b: WorkerProfile) => a.model === b.model && a.reasoningEffort === b.reasoningEffort

export function WorkerCard({ worker: w }: { worker: Worker }) {
  const { snapshot, adapter, runAction, selectTask } = useApp()
  const task = snapshot.tasks.find(t => t.id === w.taskId)
  const [draft, setDraft] = useState(w.nextProfile)
  const [saving, setSaving] = useState(false)
  useEffect(() => setDraft(w.nextProfile), [w.nextProfile.model, w.nextProfile.reasoningEffort, w.executor])
  const efforts = w.modelOptions.find(option => option.model === draft.model)?.reasoningEfforts ?? [null]
  const Icon = w.executor === 'Codex' ? Laptop : Terminal
  const pending = !same(w.profile, w.nextProfile)
  return (
    <div className="worker">
      <div className="worker-icon"><Icon size={19} /></div>
      <div className="worker-content">
        <div className="row-between">
          <strong>{w.executor} Worker {w.slot}</strong>
          <span className={`badge ${w.issue ? 'tone-amber' : 'tone-green'} badge-compact`}><span className="dot" />{w.issue ? '待配置' : 'Running'}</span>
        </div>
        {task && <>
          <button className="worker-task" onClick={() => selectTask(task.id)}>
            <span className="task-id">{task.id}</span> <span className="worker-task-name">{task.name}</span>
          </button>
          <div className="worker-meter small">
            <ProgressBar value={task.progress} tone="blue" />
            <span className="muted">{task.progress === null ? task.stage : `${Math.round(task.progress)}%`}</span>
            <span className="num muted worker-elapsed"><Clock size={12} />{formatDuration(w.elapsedSec)}</span>
          </div>
          <div className="small worker-activity">
            <span>{processLabel(task.execution?.processState ?? 'unknown')}</span>
            <span className="muted">最近输出：{activityAge(task.execution?.lastOutputAt ?? null)}</span>
          </div>
          <div className="small muted worker-operation" title={task.execution?.currentOperation ?? ''}>
            {task.execution?.currentOperation ?? '暂无操作事件，打开执行详情查看'}
          </div>
          <button className="link-btn small" onClick={() => selectTask(task.id)}>查看执行详情与实时日志</button>
        </>}
        <div className="small muted worker-current">{w.issue ? '已保存' : '当前'}：{w.profile.model ?? '执行器默认模型'} · {w.profile.reasoningEffort ?? '默认强度'}</div>
        {w.issue && <div className="small tone-text-amber">{w.issue}</div>}
        <div className="worker-settings">
          <label>模型
            <select className="select" aria-label={`Worker ${w.slot} 模型`} value={draft.model ?? ''}
              disabled={saving || !w.configurable}
              onChange={event => {
                const model = event.target.value || null
                const supported = w.modelOptions.find(option => option.model === model)?.reasoningEfforts ?? [null]
                setDraft({ model, reasoningEffort: supported.includes(draft.reasoningEffort) ? draft.reasoningEffort : supported[0] })
              }}>
              {w.modelOptions.length === 0 && <option value={draft.model ?? ''}>{w.catalogStatus === 'loading' ? '正在读取官方目录…' : '官方目录不可用'}</option>}
              {w.modelOptions.length > 0 && !w.modelOptions.some(option => option.model === draft.model) && <option disabled value={draft.model ?? ''}>当前选择已不可用</option>}
              {w.modelOptions.map(option => <option key={option.model ?? ''} value={option.model ?? ''}>{option.displayName ?? option.model ?? '执行器默认模型'}</option>)}
            </select>
          </label>
          <label>思考强度
            <select className="select" aria-label={`Worker ${w.slot} 思考强度`} value={draft.reasoningEffort ?? ''}
              disabled={saving || !w.configurable || (efforts.length < 2 && efforts.includes(draft.reasoningEffort))}
              onChange={event => setDraft({ ...draft, reasoningEffort: event.target.value || null })}>
              {!efforts.includes(draft.reasoningEffort) && <option disabled value={draft.reasoningEffort ?? ''}>当前强度已不可用</option>}
              {efforts.map(effort => <option key={effort ?? ''} value={effort ?? ''}>{effort ?? '默认'}</option>)}
            </select>
          </label>
          <button className="btn btn-sm" disabled={saving || !w.configurable || same(draft, w.nextProfile)}
            aria-label={`应用 Worker ${w.slot} 配置`} aria-busy={saving}
            onClick={async () => {
              setSaving(true)
              try {
                await runAction(() => adapter.configureWorker(w.slot, w.executor, draft), `Worker ${w.slot} 配置已保存，下次领取任务生效`)
              } finally { setSaving(false) }
            }}>{saving ? '保存中…' : '应用'}</button>
        </div>
        {w.issue && w.defaultProfile && <button className="link-btn small" disabled={saving}
          onClick={() => runAction(() => adapter.configureWorker(w.slot, w.executor, w.defaultProfile!), '已恢复配置中的默认模型；可显式恢复调度')}>恢复默认配置</button>}
        <div className="small muted">{w.catalogStatus !== 'ready' ? (w.catalogError ?? '正在读取官方模型目录…')
          : !w.configurable ? '此任务尚未绑定 Worker 编号，后续任务可配置'
          : w.issue ? '修正配置后，可显式恢复调度'
          : pending ? '已保存，将在下次领取任务时切换' : '选择用于下一任务；当前任务继续使用原配置'}</div>
        {w.catalogSource && <div className="small muted">来源：{w.catalogSource}{w.catalogUpdatedAt ? ` · ${new Date(w.catalogUpdatedAt).toLocaleTimeString()}` : ''}</div>}
      </div>
    </div>
  )
}

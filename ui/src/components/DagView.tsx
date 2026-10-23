import { useEffect, useMemo, useState } from 'react'
import {
  Background,
  BackgroundVariant,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import ELK from 'elkjs/lib/elk.bundled.js'
import { Circle, CircleCheck, CircleX, Clock, Crosshair, LoaderCircle, Maximize, Maximize2, RotateCcw, ZoomIn, ZoomOut } from 'lucide-react'
import { useApp } from './AppContext'
import { EmptyState, LoadingState, ErrorState } from './ui'
import { TaskDetail } from './TaskDetail'
import type { Task, TaskStatus } from '../types'

const FULL = { w: 210, h: 66, gapNode: 26, gapLayer: 80 }
const COMPACT = { w: 176, h: 58, gapNode: 18, gapLayer: 56 }
const elk = new ELK()

type Role = 'none' | 'selected' | 'upstream' | 'downstream' | 'dim'

interface TaskNodeData extends Record<string, unknown> {
  task: Task
  role: Role
  compact: boolean
}
type FlowNode = Node<TaskNodeData, 'task'>

const STATUS_ICON: Record<TaskStatus, typeof Circle> = {
  completed: CircleCheck,
  running: LoaderCircle,
  pending: Circle,
  waiting: Clock,
  failed: CircleX,
}

const STATUS_LABEL: Record<TaskStatus, string> = {
  completed: 'Completed',
  running: 'Running',
  pending: 'Pending',
  waiting: 'Waiting',
  failed: 'Failed',
}

const STATUS_TONE: Record<TaskStatus, string> = {
  completed: 'green',
  running: 'blue',
  pending: 'gray',
  waiting: 'gray',
  failed: 'red',
}

const LEGEND: TaskStatus[] = ['completed', 'running', 'pending', 'waiting', 'failed']

function TaskNode({ data }: NodeProps<FlowNode>) {
  const { task: t, role, compact } = data
  const Icon = STATUS_ICON[t.status]
  const detail =
    t.status === 'failed' || t.status === 'waiting' ? (
      <span className={t.status === 'failed' ? 'text-red' : 'muted'}>{t.lastError ?? t.blockedReason}</span>
    ) : (
      <span className="muted">
        {t.executor} · {t.stage}
      </span>
    )
  return (
    <div className={`dag-node st-${t.status} role-${role} ${compact ? 'is-compact' : ''}`} title={`${t.id} ${t.name}`}>
      <Handle type="target" position={Position.Left} isConnectable={false} className="dag-handle" />
      <div className="dag-node-icon">
        <Icon size={compact ? 14 : 16} className={t.status === 'running' ? 'spin' : undefined} />
      </div>
      <div className="dag-node-text">
        <div className="row-gap dag-node-head">
          <span className="task-id">{t.id}</span>
          <span className="dag-node-name">{t.name}</span>
        </div>
        {!compact && <div className="small dag-node-detail">{detail}</div>}
      </div>
      {t.status === 'running' && t.progress !== null && <div className="dag-node-bar" style={{ width: `${t.progress}%` }} />}
      <Handle type="source" position={Position.Right} isConnectable={false} className="dag-handle" />
    </div>
  )
}

const nodeTypes = { task: TaskNode }

async function layoutGraph(tasks: Task[], dims: typeof FULL): Promise<Record<string, { x: number; y: number }>> {
  const ids = new Set(tasks.map((t) => t.id))
  const result = await elk.layout({
    id: 'root',
    layoutOptions: {
      'elk.algorithm': 'layered',
      'elk.direction': 'RIGHT',
      'elk.spacing.nodeNode': String(dims.gapNode),
      'elk.layered.spacing.nodeNodeBetweenLayers': String(dims.gapLayer),
      'elk.layered.nodePlacement.strategy': 'BRANDES_KOEPF',
      'elk.layered.considerModelOrder.strategy': 'NODES_AND_EDGES',
      'elk.edgeRouting': 'ORTHOGONAL',
    },
    children: tasks.map((t) => ({ id: t.id, width: dims.w, height: dims.h })),
    edges: tasks.flatMap((t) =>
      t.deps.filter((d) => ids.has(d)).map((d) => ({ id: `${d}->${t.id}`, sources: [d], targets: [t.id] })),
    ),
  })
  const positions: Record<string, { x: number; y: number }> = {}
  result.children?.forEach((c) => {
    positions[c.id] = { x: c.x ?? 0, y: c.y ?? 0 }
  })
  return positions
}

function relations(tasks: Task[], selectedId: string | null) {
  const up = new Set<string>()
  const down = new Set<string>()
  if (!selectedId) return { up, down }
  const byId = new Map(tasks.map((t) => [t.id, t]))
  const walkUp = (id: string) => {
    byId.get(id)?.deps.forEach((d) => {
      if (!up.has(d)) {
        up.add(d)
        walkUp(d)
      }
    })
  }
  walkUp(selectedId)
  tasks.forEach((t) => {
    if (t.deps.includes(selectedId)) {
      const stack = [t.id]
      while (stack.length) {
        const cur = stack.pop()!
        if (!down.has(cur)) {
          down.add(cur)
          tasks.filter((x) => x.deps.includes(cur)).forEach((x) => stack.push(x.id))
        }
      }
    }
  })
  return { up, down }
}

function DagLegend() {
  return (
    <div className="legend">
      {LEGEND.map((s) => (
        <span key={s} className="legend-item">
          <span className={`dot tone-dot-${STATUS_TONE[s]}`} />
          {STATUS_LABEL[s]}
        </span>
      ))}
    </div>
  )
}

interface CanvasProps {
  tasks: Task[]
  compact: boolean
}

function DagCanvas({ tasks, compact }: CanvasProps) {
  const { selectedTaskId, selectTask, setTab, notify } = useApp()
  const { fitView, zoomIn, zoomOut, setCenter, getZoom } = useReactFlow()
  const dims = compact ? COMPACT : FULL
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>({})
  const [layoutState, setLayoutState] = useState<'running' | 'done' | 'error'>('running')
  const [layoutError, setLayoutError] = useState<string | null>(null)
  const [layoutNonce, setLayoutNonce] = useState(0)
  const [narrow, setNarrow] = useState(() => window.matchMedia('(max-width: 760px)').matches)

  useEffect(() => {
    const media = window.matchMedia('(max-width: 760px)')
    const update = () => setNarrow(media.matches)
    media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [])

  const structureKey = useMemo(() => tasks.map((t) => `${t.id}:${t.deps.join('|')}`).join(';'), [tasks])

  useEffect(() => {
    let cancelled = false
    setLayoutState('running')
    layoutGraph(tasks, dims)
      .then((p) => {
        if (cancelled) return
        setPositions(p)
        setLayoutError(null)
        setLayoutState('done')
      })
      .catch((e: unknown) => {
        if (cancelled) return
        setLayoutError(e instanceof Error ? e.message : String(e))
        setLayoutState('error')
      })
    return () => {
      cancelled = true
    }
    // 只在依赖结构变化或手动重排时重新布局；状态变化不会导致节点跳动
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [structureKey, layoutNonce, compact])

  useEffect(() => {
    if (layoutState !== 'done') return
    const t = window.setTimeout(() => fitView({ padding: compact ? 0.12 : 0.15, minZoom: compact && narrow ? 0.85 : 0.15, duration: compact ? 0 : 300 }), 60)
    return () => window.clearTimeout(t)
  }, [layoutState, layoutNonce, fitView, compact, narrow])

  const { up, down } = useMemo(() => relations(tasks, selectedTaskId), [tasks, selectedTaskId])

  const nodes: FlowNode[] = useMemo(
    () =>
      tasks.map((t) => {
        let role: Role = 'none'
        if (selectedTaskId) {
          if (t.id === selectedTaskId) role = 'selected'
          else if (up.has(t.id)) role = 'upstream'
          else if (down.has(t.id)) role = 'downstream'
          else role = 'dim'
        }
        return {
          id: t.id,
          type: 'task',
          position: positions[t.id] ?? { x: 0, y: 0 },
          data: { task: t, role, compact },
          width: dims.w,
          height: dims.h,
          draggable: false,
          selectable: false,
        }
      }),
    [tasks, positions, selectedTaskId, up, down, compact, dims.w, dims.h],
  )

  const edges: Edge[] = useMemo(() => {
    return tasks.flatMap((t) =>
      t.deps.map((d) => {
        const onUp = !!selectedTaskId && (d === selectedTaskId || up.has(d)) && (t.id === selectedTaskId || up.has(t.id))
        const onDown = !!selectedTaskId && (d === selectedTaskId || down.has(d)) && (t.id === selectedTaskId || down.has(t.id))
        const lit = onUp || onDown
        const active = !selectedTaskId || lit
        const color = lit ? '#79baff' : active ? 'rgba(221,230,242,0.55)' : 'rgba(221,230,242,0.12)'
        return {
          id: `${d}->${t.id}`,
          source: d,
          target: t.id,
          type: 'smoothstep',
          animated: t.status === 'running' && active,
          className: lit ? 'edge-lit' : active ? '' : 'edge-dim',
          style: {
            stroke: color,
            strokeWidth: lit ? 2 : 1.4,
            strokeDasharray: t.status === 'waiting' ? '5 4' : undefined,
          },
          markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color },
        }
      }),
    )
  }, [tasks, selectedTaskId, up, down])

  const locate = () => {
    if (!selectedTaskId || !positions[selectedTaskId]) {
      notify('请先在画布中选择一个任务', 'info')
      return
    }
    const p = positions[selectedTaskId]
    setCenter(p.x + dims.w / 2, p.y + dims.h / 2, { zoom: Math.max(getZoom(), 0.9), duration: 400 })
  }

  const selected = tasks.find((t) => t.id === selectedTaskId) ?? null
  const fit = () => fitView({ padding: compact ? 0.1 : 0.15, duration: 300 })

  if (tasks.length === 0) return <EmptyState title="暂无任务图" hint="尚未加载任务依赖关系" />

  const canvas = (
    <div className={`dag-canvas ${compact ? 'is-compact' : ''}`}>
      {layoutState === 'running' && <LoadingState text="正在计算 DAG 布局…" />}
      {layoutState === 'error' && (
        <ErrorState message={layoutError ?? '布局失败'} onRetry={() => setLayoutNonce((n) => n + 1)} />
      )}
      {layoutState === 'done' && (
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodeClick={(_, n) => {
            if (compact) {
              selectTask(n.id)
              setTab('dag')
            } else {
              selectTask(n.id)
            }
          }}
          onPaneClick={() => {
            if (!compact) selectTask(null)
          }}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable={false}
          minZoom={0.15}
          maxZoom={1.8}
          panOnScroll={false}
          zoomOnDoubleClick={false}
          proOptions={{ hideAttribution: false }}
        >
          {!compact && <Background variant={BackgroundVariant.Dots} gap={24} size={1} color="rgba(255,255,255,0.06)" />}
        </ReactFlow>
      )}

      {!compact && selected && (
        <aside className="dag-panel">
          <TaskDetail task={selected} onClose={() => selectTask(null)} compact />
        </aside>
      )}

      {!compact && <div className="dag-hint muted small">拖拽平移 · 滚轮缩放 · 点击节点查看上下游</div>}
    </div>
  )

  if (compact) {
    return (
      <div className="dag-wrap is-compact">
        <div className="dag-toolbar">
          <div className="section-heading">Task DAG</div>
          <DagLegend />
          <div className="btn-row">
            <button className="icon-btn icon-btn-bordered" title="放大" aria-label="放大" onClick={() => zoomIn({ duration: 200 })}>
              <ZoomIn size={14} />
            </button>
            <button className="icon-btn icon-btn-bordered" title="缩小" aria-label="缩小" onClick={() => zoomOut({ duration: 200 })}>
              <ZoomOut size={14} />
            </button>
            <button className="btn btn-sm" onClick={fit}>
              Fit
            </button>
            <button className="icon-btn icon-btn-bordered" title="展开 DAG 页面" aria-label="展开 DAG 页面" onClick={() => setTab('dag')}>
              <Maximize2 size={14} />
            </button>
          </div>
        </div>
        {canvas}
        <p className="compact-dag-hint">拖拽查看任务 · Fit 查看全图</p>
      </div>
    )
  }

  return (
    <div className="dag-wrap">
      <div className="dag-toolbar">
        <DagLegend />
        <div className="btn-row">
          <button className="icon-btn" title="放大" aria-label="放大" onClick={() => zoomIn({ duration: 200 })}>
            <ZoomIn size={14} />
          </button>
          <button className="icon-btn" title="缩小" aria-label="缩小" onClick={() => zoomOut({ duration: 200 })}>
            <ZoomOut size={14} />
          </button>
          <button className="btn btn-sm" onClick={fit}>
            <Maximize size={13} /> 适应画布
          </button>
          <button className="icon-btn" title="定位选中节点" aria-label="定位选中节点" onClick={locate}>
            <Crosshair size={14} />
          </button>
          <button className="icon-btn" title="重新布局" aria-label="重新布局" onClick={() => setLayoutNonce((n) => n + 1)}>
            <RotateCcw size={14} />
          </button>
        </div>
      </div>
      {canvas}
    </div>
  )
}

export function DagView() {
  const { snapshot } = useApp()
  return (
    <ReactFlowProvider>
      <DagCanvas tasks={snapshot.tasks} compact={false} />
    </ReactFlowProvider>
  )
}

export function DagOverview({ tasks }: { tasks: Task[] }) {
  return (
    <ReactFlowProvider>
      <DagCanvas tasks={tasks} compact />
    </ReactFlowProvider>
  )
}

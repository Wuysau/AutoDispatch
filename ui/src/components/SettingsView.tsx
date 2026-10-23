import { RotateCcw } from 'lucide-react'
import { useApp } from './AppContext'
import { hostIsPresent, nativeWindow, requestHostAction, useNativeWindowState } from '../data/hostBridge'
import { DEFAULT_PREFS } from '../data/prefs'
import { Glass } from './ui'

export function SettingsView() {
  const { snapshot, adapter, prefs, updatePrefs, reloadData, notify, runAction } = useApp()
  const host = hostIsPresent()
  const nativeState = useNativeWindowState('widget')

  const resetPrefs = () => {
    updatePrefs(DEFAULT_PREFS)
    notify('已恢复默认外观设置', 'success')
  }

  return (
    <div className="settings-view">
      <Glass className="card">
        <div className="card-label">外观</div>

        <SettingRow
          title="窗口透明度"
          hint={nativeWindow ? '调整桌面玻璃的烟熏深浅，文字保持清晰，设置自动保存' : '影响悬浮组件与控制台的玻璃背景透明程度，设置会自动保存'}
          value={`${Math.round(prefs.opacity * 100)}%`}
        >
          <input
            type="range"
            min={0.35}
            max={0.95}
            step={0.01}
            value={prefs.opacity}
            onChange={(e) => updatePrefs({ opacity: Number(e.target.value) })}
            aria-label="窗口透明度"
          />
        </SettingRow>

        <SettingRow title="背景模糊" hint={nativeWindow ? '真实桌面与其他窗口的模糊由 Windows 管理' : '调整页面内玻璃背景模糊'} value={nativeWindow ? '系统' : `${prefs.blur}px`}>
          {nativeWindow ? <span className="muted small">Windows 桌面材质</span> : <input
            type="range"
            min={0}
            max={40}
            step={1}
            value={prefs.blur}
            onChange={(e) => updatePrefs({ blur: Number(e.target.value) })}
            aria-label="背景模糊"
          />}
        </SettingRow>

        <SettingRow title="显示桌面悬浮组件" hint="关闭后可在此处重新打开">
          <Toggle
            checked={nativeWindow ? Boolean(nativeState?.visible) : prefs.widgetVisible}
            onChange={(v) => {
              if (nativeWindow) void runAction(() => requestHostAction(v ? 'show' : 'hide', 'widget'))
              else updatePrefs({ widgetVisible: v })
            }}
            label="显示桌面悬浮组件"
          />
        </SettingRow>

        <SettingRow title="悬浮组件默认折叠" hint="下次打开页面时以折叠状态显示">
          <Toggle
            checked={prefs.widgetCollapsed}
            onChange={(v) => updatePrefs({ widgetCollapsed: v })}
            label="悬浮组件默认折叠"
          />
        </SettingRow>

        <div className="row-end">
          <button className="btn" onClick={resetPrefs}>
            <RotateCcw size={13} /> 恢复默认
          </button>
        </div>
      </Glass>

      <Glass className="card">
        <div className="card-label">数据源与宿主</div>
        <dl className="kv">
          <dt>当前数据源</dt>
          <dd>
            {adapter.name}
            {snapshot.source === 'demo' && <span className="muted"> （模拟数据，不代表真实调度结果）</span>}
          </dd>
          <dt>刷新方式</dt>
          <dd>{snapshot.source === 'live' ? '每 2 秒轮询 /api/autodispatch/snapshot' : 'Demo 引擎本地推进（每秒）'}</dd>
          <dt>窗口宿主</dt>
          <dd>{host ? '本机原生宿主已连接' : '浏览器预览（无原生窗口能力）'}</dd>
          {nativeWindow && <>
            <dt>桌面材质</dt>
            <dd>{!host ? '正在连接宿主' : nativeState?.material?.mode === 'acrylic' ? 'Acrylic · 真实桌面透景' : nativeState?.material?.reason === 'transparency-disabled' ? '系统透明效果已关闭；可在 Windows 设置 → 辅助功能 → 视觉效果中开启' : '深色回退 · 系统未提供 Acrylic'}</dd>
          </>}
          <dt>快照时间</dt>
          <dd className="mono">{new Date(snapshot.updatedAt).toLocaleTimeString()}</dd>
        </dl>
        <div className="row-end">
          <button className="btn" onClick={reloadData}>
            重新连接数据源
          </button>
        </div>
      </Glass>
    </div>
  )
}

function SettingRow({
  title,
  hint,
  value,
  children,
}: {
  title: string
  hint: string
  value?: string
  children: React.ReactNode
}) {
  return (
    <div className="setting-row">
      <div className="setting-text">
        <div className="row-gap">
          <strong>{title}</strong>
          {value && <span className="num muted small">{value}</span>}
        </div>
        <div className="muted small">{hint}</div>
      </div>
      <div className="setting-control">{children}</div>
    </div>
  )
}

function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <button
      role="switch"
      aria-checked={checked}
      aria-label={label}
      className={`toggle ${checked ? 'is-on' : ''}`}
      onClick={() => onChange(!checked)}
    >
      <span className="toggle-knob" />
    </button>
  )
}

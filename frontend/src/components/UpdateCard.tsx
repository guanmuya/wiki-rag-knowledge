import { useEffect, useState } from 'react'
import { isDesktop } from '../api'
import type { UpdateState } from '../types'
import { Badge, Button, Spinner } from './ui'

/**
 * 关于页的「软件更新」卡片。
 *
 * 状态由主进程推送（updater.cjs），这里只负责展示与触发：
 * - 自动检查是主进程定时做的，本卡片不轮询；
 * - 检查失败不弹窗，错误原因在这里显示；
 * - 「重启并安装」只在下载完成后出现。
 */
export default function UpdateCard() {
  const [state, setState] = useState<UpdateState | null>(null)
  const [checking, setChecking] = useState(false)
  const [installing, setInstalling] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!isDesktop()) return
    let alive = true
    void window.wrkDesktop
      ?.updateState?.()
      .then((s) => {
        if (alive) setState(s)
      })
      .catch(() => {
        /* 主进程没注册更新模块时忽略 */
      })
    const off = window.wrkDesktop?.onUpdate?.((s) => setState(s))
    return () => {
      alive = false
      off?.()
    }
  }, [])

  const status = state?.status || 'idle'
  const desktop = isDesktop()

  const check = async () => {
    setChecking(true)
    setError('')
    try {
      const r = await window.wrkDesktop?.checkUpdate?.()
      if (r && r.ok === false && r.message) setError(r.message)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setChecking(false)
    }
  }

  const install = async () => {
    setInstalling(true)
    try {
      await window.wrkDesktop?.installUpdate?.()
    } finally {
      // 正常情况下应用会被 updater 关掉，这里只是兜底
      setInstalling(false)
    }
  }

  const badge = (() => {
    switch (status) {
      case 'checking':
        return <Badge tone="info">检查中</Badge>
      case 'available':
      case 'downloading':
        return <Badge tone="info">有新版本</Badge>
      case 'ready':
        return <Badge tone="warn">待安装</Badge>
      case 'none':
        return <Badge tone="success">已是最新</Badge>
      case 'error':
        return <Badge tone="danger">检查失败</Badge>
      case 'disabled':
        return <Badge tone="muted">未启用</Badge>
      default:
        return <Badge tone="muted">待检查</Badge>
    }
  })()

  const message = error || state?.message || (desktop ? '启动 8 秒后自动检查，之后每 4 小时一次' : '浏览器模式不支持自动更新')

  return (
    <div className="card">
      <div className="card-head">
        <h3>软件更新</h3>
        <div className="spacer" />
        {badge}
      </div>
      <div className="card-body">
        <dl className="info-list">
          <dt>当前版本</dt>
          <dd className="mono">v{state?.currentVersion || '—'}</dd>
          <dt>最新版本</dt>
          <dd className="mono">{state?.latestVersion ? `v${state.latestVersion}` : '—'}</dd>
          <dt>更新源</dt>
          <dd className="mono">{state?.feed || '未配置'}</dd>
          <dt>最近检查</dt>
          <dd>{state?.checkedAt ? new Date(state.checkedAt).toLocaleString('zh-CN') : '尚未检查'}</dd>
        </dl>

        {status === 'downloading' ? (
          <div style={{ marginTop: 12 }}>
            <div className="progress">
              <div className="progress-fill" style={{ width: `${state?.percent || 0}%` }} />
              <span className="progress-text">{state?.percent || 0}%</span>
            </div>
          </div>
        ) : null}

        <div
          className={status === 'error' ? 'insufficient-banner' : 'small muted'}
          style={{ marginTop: 12 }}
        >
          {status === 'error' ? <span>⚠</span> : null}
          <span style={{ flex: 1 }}>{message}</span>
        </div>

        <div className="divider" />
        <div className="row row-wrap" style={{ gap: 8 }}>
          <Button
            size="sm"
            onClick={() => void check()}
            loading={checking || status === 'checking'}
            disabled={!desktop || status === 'downloading'}
          >
            检查更新
          </Button>
          {status === 'ready' ? (
            <Button size="sm" variant="primary" loading={installing} onClick={() => void install()}>
              重启并安装 v{state?.latestVersion}
            </Button>
          ) : null}
          {status === 'checking' || status === 'downloading' ? (
            <span className="inline tiny muted">
              <Spinner size={12} /> 正在处理…
            </span>
          ) : null}
        </div>
      </div>
    </div>
  )
}

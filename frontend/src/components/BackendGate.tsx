import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { getApiBase, isDesktop, probeBackend, metaApi, SETUP_COMPLETED_EVENT } from '../api'
import { useAuth } from '../auth'
import { LOG_EMPTY, LOG_READ_FAILED } from '../shared/messages'
import { probeFailureHint } from '../shared/userFacingErrors'
import type { DesktopInfo, StartupFailure } from '../types'
import { Spinner } from './ui'

interface ProbeState {
  ok: boolean
  message: string
  version?: string
  latencyMs: number
}

// 本地兜底文案不在这里写：主进程没给失败信息时（浏览器模式、探活超时等），
// 文案取自 src/shared/userFacingErrors.ts → electron/error-hints.json 的 probe 段，
// 与主进程的启动失败翻译是同一份表（体检清单 P2-6：改一处，三处同步生效）。

/**
 * 启动门：后端不可用时不要白屏。
 * - 桌面版：先确认 preload 拿到的 apiBase 有效，再探活 /api/meta。
 * - 浏览器版：直接探活（走 Vite 代理）。
 * 只在首次挂载时判定一次；进入系统后后端抖动由各页面的错误态处理。
 *
 * 另外负责「首次初始化」跳转：后端能连但还没有任何管理员时，
 * 除了 /setup 以外的页面一律重定向到引导页。这个判断放在门里而不是各页面，
 * 避免用户在未初始化状态下看到一堆 401/403。
 */
export default function BackendGate({ children }: { children: ReactNode }) {
  const [phase, setPhase] = useState<'checking' | 'ready' | 'down'>('checking')
  const [probe, setProbe] = useState<ProbeState | null>(null)
  const [desktopInfo, setDesktopInfo] = useState<DesktopInfo | null>(null)
  const [failure, setFailure] = useState<StartupFailure | null>(null)
  const [logs, setLogs] = useState<string>('')
  const [logsOpen, setLogsOpen] = useState(false)
  const [detailOpen, setDetailOpen] = useState(false)
  const [logsLoading, setLogsLoading] = useState(false)
  const [needsSetup, setNeedsSetup] = useState(false)
  const location = useLocation()
  const { mustChangePassword } = useAuth()
  // P1-9：记录是否见过「必须改密」状态；只有 true → false（即改密成功）时
  // 才通知主进程清除内存里的初始凭据，避免未登录时初始的 false 误触发。
  const hadMustChange = useRef(false)

  /** P1-9：首次登录改密成功后，清掉主进程内存中的初始管理员凭据。 */
  useEffect(() => {
    if (mustChangePassword) {
      hadMustChange.current = true
      return
    }
    if (!hadMustChange.current) return
    hadMustChange.current = false
    void Promise.resolve(window.wrkDesktop?.clearInitialCredential()).catch(() => {
      /* 浏览器模式没有 preload，忽略 */
    })
  }, [mustChangePassword])

  /** 问主进程要「这次启动到底为什么失败」，拿不到就返回 null。 */
  const readFailure = useCallback(async (): Promise<StartupFailure | null> => {
    if (!isDesktop()) return null
    try {
      return (await window.wrkDesktop?.getFailure()) ?? null
    } catch {
      return null
    }
  }, [])

  const check = useCallback(async () => {
    setPhase('checking')
    setProbe(null)
    setFailure(null)

    // 桌面版：apiBase 为空说明 sidecar 没起来，直接判失败，避免请求打到 file://
    if (isDesktop()) {
      try {
        const info = await window.wrkDesktop?.getInfo()
        if (info) setDesktopInfo(info)
        if (!info?.apiBase) {
          // sidecar 起不来时，主进程已经记下了具体原因（退出码 / 端口 / 权限），
          // 这里取出来直接用，比「sidecar 未启动」这句废话有用得多。
          setFailure(await readFailure())
          setProbe({
            ok: false,
            message: '未获取到后端服务地址（sidecar 未启动）',
            latencyMs: 0,
          })
          setPhase('down')
          return
        }
      } catch {
        setProbe({ ok: false, message: '无法从桌面主进程获取后端信息', latencyMs: 0 })
        setPhase('down')
        return
      }
    }

    const result = await probeBackend(6000)
    setProbe(result)
    if (result.ok) {
      // 探活通过后再问一次是否需要初始化；失败时按已初始化处理，
      // 让用户看到登录页的 401 提示，而不是卡在引导页
      try {
        const meta = await metaApi.get()
        setNeedsSetup(Boolean(meta.needs_setup))
      } catch {
        setNeedsSetup(false)
      }
    } else {
      // 进程在但接口不通（或刚崩）时，主进程的失败说明仍然最有参考价值
      setFailure(await readFailure())
    }
    setPhase(result.ok ? 'ready' : 'down')
  }, [readFailure])

  useEffect(() => {
    void check()
  }, [check])

  // 创建成功的响应已确认初始化完成；立即清除缓存，避免跳转时被旧状态弹回。
  // 下次启动或刷新页面时，check() 仍会重新向后端确认状态。
  useEffect(() => {
    const handler = () => setNeedsSetup(false)
    window.addEventListener(SETUP_COMPLETED_EVENT, handler)
    return () => window.removeEventListener(SETUP_COMPLETED_EVENT, handler)
  }, [])

  const openLogs = async () => {
    setLogsOpen(true)
    if (!isDesktop()) return
    try {
      const text = await window.wrkDesktop?.getLogs()
      setLogs(text || LOG_EMPTY)
    } catch {
      setLogs(LOG_READ_FAILED)
    }
  }

  const copyLogs = async () => {
    setLogsLoading(true)
    try {
      const text = logs || (await window.wrkDesktop?.getLogs()) || ''
      await navigator.clipboard.writeText(text || LOG_EMPTY)
    } catch {
      /* 剪贴板不可用时忽略 */
    } finally {
      setLogsLoading(false)
    }
  }

  if (phase === 'checking') {
    return (
      <div className="boot-screen">
        <div className="state-block">
          <Spinner size={22} />
          <span>正在连接后端服务…</span>
        </div>
      </div>
    )
  }

  if (phase === 'ready') {
    // 未初始化：除引导页外全部重定向。已初始化却停在引导页则送回登录页。
    if (needsSetup && location.pathname !== '/setup') {
      return <Navigate to="/setup" replace />
    }
    if (!needsSetup && location.pathname === '/setup') {
      return <Navigate to="/login" replace />
    }
    return <>{children}</>
  }

  const rawMessage = probe?.message || '无法连接后端服务'
  // 主进程的解释优先（它知道退出码）；没有才用本地兜底
  const headline = failure
    ? { title: failure.title, action: failure.action }
    : probeFailureHint(rawMessage)
  const technicalDetail = failure?.detail || rawMessage

  return (
    <div className="boot-screen">
      <div className="backend-down">
        <div className="backend-down-icon">!</div>
        <h1>{headline.title}</h1>
        <p className="backend-down-msg">{headline.action}</p>

        <div className="backend-down-actions">
          <button type="button" className="btn btn-primary" onClick={() => void check()}>
            重试连接
          </button>
          {isDesktop() ? (
            <button
              type="button"
              className="btn"
              onClick={async () => {
                try {
                  await window.wrkDesktop?.restartBackend()
                } catch {
                  /* 忽略 */
                }
                await check()
              }}
            >
              重启后端
            </button>
          ) : null}
          <button type="button" className="btn" onClick={() => setDetailOpen((v) => !v)}>
            {detailOpen ? '收起详情' : '查看详情'}
          </button>
        </div>

        {detailOpen ? (
          <div className="backend-down-logs">
            <dl className="backend-down-meta">
              <dt>错误信息</dt>
              <dd className="mono">{technicalDetail}</dd>
              <dt>连接地址</dt>
              <dd className="mono">{getApiBase() || '（浏览器模式：/api 经 Vite 代理）'}</dd>
              <dt>运行模式</dt>
              <dd>{isDesktop() ? '桌面版（Electron + sidecar）' : '浏览器开发模式'}</dd>
              {desktopInfo?.dataDir ? (
                <>
                  <dt>数据目录</dt>
                  <dd className="mono">{desktopInfo.dataDir}</dd>
                </>
              ) : null}
            </dl>

            <div className="backend-down-tips">
              <div className="tiny strong">排查顺序</div>
              <ol>
                <li>确认后端进程已启动（浏览器模式默认 http://127.0.0.1:8000）</li>
                <li>桌面版点「重启后端」，或从「文件 → 打开日志」查看 sidecar 报错</li>
                <li>端口被占用时桌面版会自动换端口；浏览器模式需先释放 8000</li>
              </ol>
            </div>

            <div className="row" style={{ marginBottom: 8 }}>
              <button type="button" className="btn-link" onClick={() => void openLogs()}>
                {logsOpen ? '刷新日志' : '加载日志'}
              </button>
              {logsOpen ? (
                <>
                  <span className="spacer" />
                  <button
                    type="button"
                    className="btn-link"
                    onClick={() => void copyLogs()}
                    disabled={logsLoading}
                  >
                    {logsLoading ? '复制中…' : '复制'}
                  </button>
                </>
              ) : null}
            </div>

            {logsOpen ? (
              <pre className="context-preview" style={{ maxHeight: 260 }}>
                {logs || '（暂无日志；浏览器开发模式下请查看启动 uvicorn 的终端）'}
              </pre>
            ) : (
              <div className="tiny muted">日志默认折叠，需要时点「加载日志」。</div>
            )}
          </div>
        ) : null}
      </div>
    </div>
  )
}

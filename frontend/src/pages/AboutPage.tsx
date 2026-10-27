import { useCallback, useEffect, useState } from 'react'
import { getApiBase, isDesktop, metaApi, probeBackend } from '../api'
import { useAuth } from '../auth'
import { resetOfflineBannerDismiss } from '../components/OfflineBanner'
import type { DesktopInfo, MetaInfo } from '../types'
import { Badge, Button, ErrorState, SkeletonLines, Spinner } from '../components/ui'
import { formatMs } from '../utils'
import { PROJECT_LICENSE, THIRD_PARTY_NOTICES } from './aboutInfo'

interface ProbeResult {
  ok: boolean
  message: string
  version?: string
  offlineModels?: boolean
  latencyMs: number
}

/**
 * 主进程补给的运行环境信息。
 * `api.ts` 的 `DesktopInfo` 只声明业务必需字段；关于页要展示安装位置、
 * 各层版本号，属于诊断用途，这里单独扩展读取，不改 api.ts 的契约。
 */
interface DesktopInfoExtended extends DesktopInfo {
  installDir?: string
  installDirExe?: string
  logDir?: string
  electronVersion?: string
  chromeVersion?: string
  nodeVersion?: string
  platform?: string
  isPackaged?: boolean
}

type OpenKind = 'data' | 'logs' | 'install'

export default function AboutPage() {
  const { user, isAdmin } = useAuth()
  const [meta, setMeta] = useState<MetaInfo | null>(null)
  const [metaLoading, setMetaLoading] = useState(true)
  const [metaError, setMetaError] = useState<unknown>(null)
  const [desktopInfo, setDesktopInfo] = useState<DesktopInfoExtended | null>(null)
  const [probe, setProbe] = useState<ProbeResult | null>(null)
  const [probing, setProbing] = useState(false)
  const [logHint, setLogHint] = useState('')
  const [logsOpen, setLogsOpen] = useState(false)
  const [noticesOpen, setNoticesOpen] = useState(false)

  const loadMeta = useCallback(async () => {
    setMetaLoading(true)
    try {
      const data = await metaApi.get()
      setMeta(data)
      setMetaError(null)
    } catch (err) {
      setMetaError(err)
    } finally {
      setMetaLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadMeta()
  }, [loadMeta])

  useEffect(() => {
    if (!isDesktop()) return
    void window.wrkDesktop
      ?.getInfo()
      .then((info) => setDesktopInfo(info as DesktopInfoExtended))
      .catch(() => setDesktopInfo(null))
  }, [])

  const checkBackend = async () => {
    setProbing(true)
    try {
      const res = await probeBackend(6000)
      setProbe(res)
    } finally {
      setProbing(false)
    }
  }

  useEffect(() => {
    void checkBackend()
  }, [])

  const openPath = async (kind: OpenKind) => {
    if (!isDesktop()) {
      setLogHint(
        kind === 'install'
          ? '浏览器开发模式没有安装目录；桌面版显示实际安装位置。'
          : '浏览器开发模式无法直接打开目录；桌面版支持。',
      )
      return
    }
    try {
      await window.wrkDesktop?.openPath(kind as 'data' | 'logs')
      setLogHint('')
    } catch {
      setLogHint('打开目录失败，请检查桌面主进程日志。')
    }
  }

  const openLogsInline = async () => {
    setLogsOpen(true)
    if (!isDesktop()) {
      setLogHint('浏览器开发模式请查看启动 uvicorn 的终端输出。')
      return
    }
    try {
      const text = await window.wrkDesktop?.getLogs()
      setLogHint(text && text.trim() ? text.slice(-2000) : '（日志为空）')
    } catch {
      setLogHint('读取日志失败')
    }
  }

  const appVersion = desktopInfo?.version || '1.0.0'
  const electronVersion = desktopInfo?.electronVersion || ''
  const installDir = desktopInfo?.installDirExe || desktopInfo?.installDir || ''

  return (
    <>
      <div className="page-head">
        <div>
          <h1>关于</h1>
          <div className="sub">版本信息、安装位置、运行环境与后端连通性。用于排查部署问题。</div>
        </div>
        <div className="spacer" />
        <div className="page-head-actions">
          <Button variant="ghost" onClick={() => void loadMeta()}>
            刷新信息
          </Button>
          <Button variant="primary" loading={probing} onClick={() => void checkBackend()}>
            检查后端状态
          </Button>
        </div>
      </div>

      <div className="settings-grid">
        <div className="card">
          <div className="card-head">
            <h3>版本信息</h3>
          </div>
          <div className="card-body">
            {metaLoading ? (
              <SkeletonLines rows={5} />
            ) : metaError ? (
              <ErrorState error={metaError} onRetry={() => void loadMeta()} />
            ) : (
              <dl className="info-list">
                <dt>应用名称</dt>
                <dd>{meta?.app_name || '企业知识库'}</dd>
                <dt>应用版本</dt>
                <dd className="mono">
                  v{appVersion}
                  {desktopInfo?.isPackaged === false ? (
                    <span className="tiny muted"> （开发模式）</span>
                  ) : null}
                </dd>
                <dt>前端版本</dt>
                <dd className="mono">
                  v{appVersion}
                  <span className="tiny muted">
                    {' '}
                    · {isDesktop() ? '随应用打包（asar）' : '浏览器模式（Vite 开发服务器）'}
                  </span>
                </dd>
                <dt>后端版本</dt>
                <dd className="mono">
                  {meta?.version ? `v${meta.version}` : '—'}
                  {desktopInfo?.backendVersion ? (
                    <span className="tiny muted"> · sidecar v{desktopInfo.backendVersion}</span>
                  ) : null}
                </dd>
                <dt>Electron 版本</dt>
                <dd className="mono">
                  {electronVersion ? (
                    <>
                      v{electronVersion}
                      {desktopInfo?.chromeVersion ? (
                        <span className="tiny muted"> · Chromium {desktopInfo.chromeVersion}</span>
                      ) : null}
                      {desktopInfo?.nodeVersion ? (
                        <span className="tiny muted"> · Node {desktopInfo.nodeVersion}</span>
                      ) : null}
                    </>
                  ) : (
                    <span className="muted">浏览器模式无 Electron</span>
                  )}
                </dd>
                <dt>系统初始化</dt>
                <dd>
                  {meta?.initialized ? (
                    <Badge tone="success">已完成</Badge>
                  ) : (
                    <Badge tone="warn">未初始化（无管理员账号）</Badge>
                  )}
                </dd>
                <dt>模型状态</dt>
                <dd>
                  {meta?.offline_models ? (
                    <Badge tone="warn">离线演示模式</Badge>
                  ) : (
                    <Badge tone="success">已配置模型</Badge>
                  )}
                  {meta?.capabilities ? (
                    <span className="tiny muted">
                      {' '}
                      · 对话 {meta.capabilities.llm_configured ? '已配置' : '未配置'} · 向量{' '}
                      {meta.capabilities.embedding_configured ? '已配置' : '未配置'} · Worker{' '}
                      {meta.capabilities.worker_enabled ? '启用' : '停用'}
                    </span>
                  ) : null}
                </dd>
                <dt>当前登录</dt>
                <dd>
                  {user?.display_name || user?.username}
                  <span className="tiny muted"> · {isAdmin ? '管理员' : '普通员工'}</span>
                </dd>
              </dl>
            )}
          </div>
        </div>

        <div className="col" style={{ gap: 16 }}>
          <div className="card">
            <div className="card-head">
              <h3>安装位置与目录</h3>
            </div>
            <div className="card-body">
              <dl className="info-list">
                <dt>安装位置</dt>
                <dd>
                  {installDir ? (
                    <button
                      type="button"
                      className="btn-link mono"
                      onClick={() => void openPath('install')}
                      title="点击打开安装目录"
                    >
                      {installDir}
                    </button>
                  ) : (
                    <span className="muted">浏览器模式不可用（没有安装目录）</span>
                  )}
                </dd>
                <dt>数据目录</dt>
                <dd>
                  {desktopInfo?.dataDir ? (
                    <>
                      <button
                        type="button"
                        className="btn-link mono"
                        onClick={() => void openPath('data')}
                        title="点击打开数据目录"
                      >
                        {desktopInfo.dataDir}
                      </button>
                      <div className="tiny muted">
                        知识库文件、数据库与密钥都在这里。卸载重装不会删除，重装后可直接继续使用。
                      </div>
                    </>
                  ) : (
                    <span className="muted">浏览器模式不可用</span>
                  )}
                </dd>
                <dt>日志目录</dt>
                <dd>
                  {desktopInfo?.logDir ? (
                    <button
                      type="button"
                      className="btn-link mono"
                      onClick={() => void openPath('logs')}
                      title="点击打开日志目录"
                    >
                      {desktopInfo.logDir}
                    </button>
                  ) : (
                    <span className="muted">浏览器模式不可用（日志在启动 uvicorn 的终端里）</span>
                  )}
                  <div className="row row-wrap" style={{ gap: 8, marginTop: 6 }}>
                    <button type="button" className="btn-link" onClick={() => void openPath('logs')}>
                      打开日志目录
                    </button>
                    <span className="muted">·</span>
                    <button type="button" className="btn-link" onClick={() => void openLogsInline()}>
                      {logsOpen ? '刷新日志内容' : '查看日志内容'}
                    </button>
                  </div>
                </dd>
                <dt>运行模式</dt>
                <dd>{isDesktop() ? '桌面版（Electron + 内置后端）' : '浏览器开发模式'}</dd>
                <dt>后端地址</dt>
                <dd className="mono">{getApiBase() || '（相对路径 /api，经 Vite 代理）'}</dd>
                {desktopInfo?.platform ? (
                  <>
                    <dt>系统平台</dt>
                    <dd className="mono">{desktopInfo.platform}</dd>
                  </>
                ) : null}
              </dl>
              {logsOpen && logHint ? (
                <pre className="context-preview" style={{ marginTop: 10, maxHeight: 200 }}>
                  {logHint}
                </pre>
              ) : null}
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <h3>后端连通性</h3>
              <div className="spacer" />
              {probe ? (
                <Badge tone={probe.ok ? 'success' : 'danger'}>
                  {probe.ok ? '正常' : '不可用'}
                </Badge>
              ) : null}
            </div>
            <div className="card-body">
              {probing && !probe ? (
                <div className="state-block inline">
                  <Spinner size={14} /> 正在检测…
                </div>
              ) : probe ? (
                <>
                  <dl className="info-list">
                    <dt>检测结果</dt>
                    <dd className={probe.ok ? 'test-ok' : 'test-fail'}>{probe.message}</dd>
                    <dt>往返耗时</dt>
                    <dd>{formatMs(probe.latencyMs)}</dd>
                    <dt>接口版本</dt>
                    <dd>{probe.version ? `v${probe.version}` : '—'}</dd>
                  </dl>
                  {!probe.ok ? (
                    <div className="insufficient-banner" style={{ marginTop: 12 }}>
                      <span>⚠</span>
                      <div style={{ flex: 1 }}>
                        后端不可用。浏览器模式请确认 uvicorn 已在 8000 端口启动；桌面版可点击「打开日志」查看 sidecar 报错。
                      </div>
                    </div>
                  ) : null}
                </>
              ) : (
                <div className="muted small">尚未检测</div>
              )}
              <div className="divider" />
              <div className="row row-wrap" style={{ gap: 8 }}>
                <Button size="sm" onClick={() => void checkBackend()} loading={probing}>
                  重新检测
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    resetOfflineBannerDismiss()
                    setLogHint('已重置「离线提示不再提示」，刷新页面后提示条会重新出现。')
                    setLogsOpen(true)
                  }}
                >
                  重置离线提示
                </Button>
              </div>
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <h3>许可证与致谢</h3>
              <div className="spacer" />
              <button
                type="button"
                className="btn-link"
                onClick={() => setNoticesOpen((v) => !v)}
              >
                {noticesOpen ? '收起第三方清单' : '展开第三方清单'}
              </button>
            </div>
            <div className="card-body">
              <dl className="info-list">
                <dt>本产品</dt>
                <dd>
                  {PROJECT_LICENSE.name}
                  <div className="tiny muted">{PROJECT_LICENSE.summary}</div>
                </dd>
                <dt>随包许可证</dt>
                <dd>
                  安装目录下的 <span className="mono">LICENSE.electron.txt</span> 与{' '}
                  <span className="mono">LICENSES.chromium.html</span>
                  <div className="tiny muted">
                    分别覆盖 Electron 与 Chromium 及其内置组件；Python 侧组件的许可证随
                    <span className="mono"> resources/backend </span>
                    一同分发。
                  </div>
                </dd>
              </dl>
              {noticesOpen ? (
                <div className="table-wrap" style={{ marginTop: 10 }}>
                  <table className="table">
                    <thead>
                      <tr>
                        <th>组件</th>
                        <th>许可证</th>
                        <th>用途</th>
                      </tr>
                    </thead>
                    <tbody>
                      {THIRD_PARTY_NOTICES.map((n) => (
                        <tr key={n.name}>
                          <td className="mono">{n.name}</td>
                          <td>{n.license}</td>
                          <td className="muted small">{n.purpose}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <div className="tiny muted">
                  共 {THIRD_PARTY_NOTICES.length} 个运行时组件，点右上角展开清单。
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </>
  )
}

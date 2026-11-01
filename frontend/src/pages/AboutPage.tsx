import { useCallback, useEffect, useState } from 'react'
import { getApiBase, isDesktop, metaApi, probeBackend } from '../api'
import { useAuth } from '../auth'
import { resetOfflineBannerDismiss } from '../components/OfflineBanner'
import type { DesktopInfo, MetaInfo } from '../types'
import { Badge, Button, ConfirmDialog, ErrorState, SkeletonLines, Spinner } from '../components/ui'
import UpdateCard from '../components/UpdateCard'
import { errorText, formatMs } from '../utils'
import { PROJECT_LICENSE, THIRD_PARTY_NOTICES } from './aboutInfo'

interface ProbeResult {
  ok: boolean
  message: string
  version?: string
  offlineModels?: boolean
  latencyMs: number
  /** 探活失败但后端其实在写库/索引：文案要说「稍后恢复」，不能说「服务挂了」 */
  busy?: boolean
}

/**
 * 主进程补给的运行环境信息。
 * `api.ts` 的 `DesktopInfo` 只声明业务必需字段；关于页要展示安装位置、
 * 各层版本号，属于诊断用途，这里单独扩展读取，不改 api.ts 的契约。
 * `pid` / `backendPid` 由新版主进程上报，旧版没有，所以是可选的。
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
  pid?: number
  backendPid?: number
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
  // 后端诊断：重启是一次破坏性操作（会中断正在跑的任务），先确认再执行
  const [restartOpen, setRestartOpen] = useState(false)
  const [restarting, setRestarting] = useState(false)
  const [restartHint, setRestartHint] = useState('')
  // 诊断卡里的「锁诊断」：把日志尾巴直接摊开，省得用户去目录里翻文件
  const [diagLogs, setDiagLogs] = useState('')
  // 主进程没上报 pid 时的兜底：从启动日志里解析出来的后端进程号
  const [logPid, setLogPid] = useState<number | null>(null)

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

  /**
   * 后端 pid 的两个来源，按可靠性排序：
   * 1) 新版主进程在 `wrk:info` 里直接给（backendPid / pid）；
   * 2) 旧版主进程不给，但日志里有一行「后端就绪：{... "pid": N}」——sidecar 的
   *    WRK_READY payload 本来就带 pid，从这里捞出来，用户不用升级也能看到进程号。
   * 都拿不到才显示「未上报」，不留一个空值让人猜。
   */
  useEffect(() => {
    if (!isDesktop() || desktopInfo?.backendPid || desktopInfo?.pid) return
    let alive = true
    void window.wrkDesktop
      ?.getLogs()
      .then((text) => {
        const pid = parsePidFromLogs(text)
        if (alive && pid) setLogPid(pid)
      })
      .catch(() => {
        /* 读不到日志不影响其它诊断信息 */
      })
    return () => {
      alive = false
    }
  }, [desktopInfo])

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

  /**
   * 锁诊断：把日志尾巴摊在诊断卡里。
   * 写锁问题的证据（`database is locked`、重试次数、持锁任务）都只在日志里，
   * 让用户点一下就能看到，比让他去目录里找文件现实得多。
   */
  const openDiagLogs = async () => {
    if (!isDesktop()) {
      setDiagLogs('浏览器开发模式请查看启动 uvicorn 的终端输出。')
      return
    }
    try {
      const text = await window.wrkDesktop?.getLogs()
      setDiagLogs(text && text.trim() ? text.slice(-2000) : '（日志为空）')
    } catch {
      setDiagLogs('读取日志失败，可点「打开日志目录」直接查看文件。')
    }
  }

  const appVersion = desktopInfo?.version || '1.0.0'
  const electronVersion = desktopInfo?.electronVersion || ''
  const installDir = desktopInfo?.installDirExe || desktopInfo?.installDir || ''
  // 后端进程号：新版主进程会带上；旧版从日志兜底，两个都没有才说「未上报」
  const reportedPid = desktopInfo?.backendPid ?? desktopInfo?.pid ?? null
  const backendPid = reportedPid ?? logPid

  /**
   * 一键重启内置后端。
   * 为什么要有：卡在「正在写入数据库」时，重启 sidecar 是用户唯一能自己做的恢复动作，
   * 否则只能卸载重装或去翻日志。重启后立刻重新探活 + 重新取版本信息，界面不用手刷。
   */
  const restartBackend = async () => {
    if (!isDesktop()) {
      setRestartHint('浏览器开发模式没有内置后端进程；请重启启动 uvicorn 的终端。')
      return
    }
    setRestarting(true)
    setRestartHint('')
    try {
      const res = await window.wrkDesktop?.restartBackend()
      if (res?.ok) {
        setRestartHint(`后端已重启${res.port ? `（端口 ${res.port}）` : ''}，正在重新检测…`)
        await loadMeta()
        await checkBackend()
      } else {
        setRestartHint(
          `重启失败：${res?.title || res?.error || '未知原因'}。可点「打开日志目录」查看 sidecar 报错。`,
        )
      }
    } catch (err) {
      setRestartHint(`重启失败：${errorText(err)}`)
    } finally {
      setRestarting(false)
      setRestartOpen(false)
    }
  }

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

        <UpdateCard />

        <div className="col" style={{ gap: 16 }}>
          {/*
            后端诊断：只在桌面版显示（浏览器模式没有内置 sidecar，这些按钮没有意义）。
            目的是「出问题时用户自己能在这一屏处理完」：看清进程、目录、版本，
            卡住了就重启后端，还不行就打开日志目录把文件发给管理员。
          */}
          {isDesktop() ? (
            <div className="card">
              <div className="card-head">
                <h3>后端诊断</h3>
                <div className="spacer" />
                {probe ? (
                  <Badge tone={probe.ok ? 'success' : probe.busy ? 'warn' : 'danger'}>
                    {probe.ok ? '运行中' : probe.busy ? '繁忙' : '未响应'}
                  </Badge>
                ) : null}
              </div>
              <div className="card-body">
                <dl className="info-list">
                  <dt>后端进程 PID</dt>
                  <dd className="mono">
                    {backendPid ? (
                      <>
                        {backendPid}
                        {!reportedPid ? (
                          <span className="tiny muted"> （取自启动日志）</span>
                        ) : null}
                      </>
                    ) : (
                      <span className="muted">
                        主进程未上报；可在下方「查看锁诊断日志」里搜「后端就绪」那一行
                      </span>
                    )}
                  </dd>
                  <dt>后端版本</dt>
                  <dd className="mono">
                    {meta?.version ? `v${meta.version}` : '—'}
                    {desktopInfo?.backendVersion ? (
                      <span className="tiny muted"> · sidecar v{desktopInfo.backendVersion}</span>
                    ) : null}
                  </dd>
                  <dt>服务地址</dt>
                  <dd className="mono">{getApiBase() || '—'}</dd>
                  <dt>数据目录</dt>
                  <dd>
                    {desktopInfo?.dataDir ? (
                      <button
                        type="button"
                        className="btn-link mono"
                        onClick={() => void openPath('data')}
                        title="点击打开数据目录"
                      >
                        {desktopInfo.dataDir}
                      </button>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </dd>
                  <dt>日志目录</dt>
                  <dd>
                    <button type="button" className="btn-link" onClick={() => void openPath('logs')}>
                      打开日志目录
                    </button>
                  </dd>
                </dl>
                <div className="row row-wrap" style={{ gap: 8, marginTop: 10 }}>
                  <Button
                    size="sm"
                    variant="primary"
                    loading={restarting}
                    onClick={() => setRestartOpen(true)}
                  >
                    重启后端
                  </Button>
                  <Button size="sm" loading={probing} onClick={() => void checkBackend()}>
                    重新检测
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => void openDiagLogs()}>
                    查看锁诊断日志
                  </Button>
                </div>
                <div className="tiny muted" style={{ marginTop: 8 }}>
                  后端卡在「正在写入数据库」时，重启可以立刻恢复；重启会中断正在运行的解析/索引任务，
                  稍后可在任务中心点重试。重启不会动数据目录里的文件与数据库。
                </div>
                {diagLogs ? (
                  <pre className="context-preview" style={{ marginTop: 10, maxHeight: 220 }}>
                    {diagLogs}
                  </pre>
                ) : null}
                {restartHint ? (
                  <div className="tiny" style={{ marginTop: 6, color: 'var(--c-text-secondary)' }}>
                    {restartHint}
                  </div>
                ) : null}
              </div>
            </div>
          ) : null}

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
                <Badge tone={probe.ok ? 'success' : probe.busy ? 'warn' : 'danger'}>
                  {probe.ok ? '正常' : probe.busy ? '繁忙' : '不可用'}
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
                    <dd className={probe.ok ? 'test-ok' : probe.busy ? '' : 'test-fail'}>
                      {probe.message}
                    </dd>
                    <dt>往返耗时</dt>
                    <dd>{formatMs(probe.latencyMs)}</dd>
                    <dt>接口版本</dt>
                    <dd>{probe.version ? `v${probe.version}` : '—'}</dd>
                  </dl>
                  {/*
                    区分「忙」和「挂了」：向量化期间后端会长时间占用写事务，
                    探活超时并不代表服务死了，说成「不可用」会让用户直接去重启/重装。
                  */}
                  {!probe.ok ? (
                    probe.busy ? (
                      <div className="task-hint" style={{ marginTop: 12 }}>
                        <span className="task-hint-icon">⏳</span>
                        <span>
                          后端仍在运行，只是暂时忙不过来（正在写入/向量化）。稍后会自动恢复，
                          可继续浏览其它页面；持续超过几分钟再考虑「重启后端」。
                        </span>
                      </div>
                    ) : (
                      <div className="insufficient-banner" style={{ marginTop: 12 }}>
                        <span>⚠</span>
                        <div style={{ flex: 1 }}>
                          后端不可用。浏览器模式请确认 uvicorn 已在 8000 端口启动；桌面版可点击「打开日志」查看
                          sidecar 报错，或在下方「后端诊断」里重启后端。
                        </div>
                      </div>
                    )
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

      <ConfirmDialog
        open={restartOpen}
        title="重启后端"
        message={
          <>
            重启会中断正在运行的解析/索引任务（稍后可在任务中心点重试），数据目录里的文件与数据库不受影响。
            确认现在重启后端？
          </>
        }
        confirmText="重启后端"
        busy={restarting}
        onConfirm={() => void restartBackend()}
        onCancel={() => setRestartOpen(false)}
      />
    </>
  )
}

/**
 * 从主进程日志里捞后端 pid。
 * 日志形如 `后端就绪：{"host":"127.0.0.1","port":53211","version":"1.0.0","pid":4321}`，
 * 取最后一次出现（重启后会再打一行），拿不到就返回 null。
 */
function parsePidFromLogs(text: string | null | undefined): number | null {
  if (!text) return null
  const matches = Array.from(text.matchAll(/"pid"\s*:\s*(\d+)/g))
  const last = matches[matches.length - 1]
  if (!last) return null
  const pid = Number(last[1])
  return Number.isFinite(pid) && pid > 0 ? pid : null
}

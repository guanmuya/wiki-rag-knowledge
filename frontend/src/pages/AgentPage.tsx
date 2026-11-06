import { useEffect, useMemo, useRef, useState } from 'react'
import { agentApi, kbApi } from '../api'
import type {
  AgentCore,
  AgentMessage,
  AgentModelOption,
  AgentPluginError,
  AgentSession,
  AgentSessionDetail,
  EvidenceItem,
  KnowledgeBase,
  UsageInfo,
} from '../types'
import {
  Badge,
  Button,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Field,
  LoadingBlock,
  Modal,
  Select,
  Spinner,
  TextInput,
} from '../components/ui'
import {
  IconAgent,
  IconAlert,
  IconPlug,
  IconPlus,
  IconRefresh,
  IconStop,
  IconTerminal,
} from '../components/icons'
import { EvidencePanel, UsageLine } from '../components/Evidence'
import { Markdown } from '../components/Markdown'
import { errorText, formatMs, formatTime } from '../utils'

interface LiveRun {
  runId: number
  sessionId: number
  text: string
  logs: string[]
  evidence: EvidenceItem[]
  stage: string
  status: 'running' | 'ok' | 'failed' | 'stopped'
  error: string
}

const EXAMPLE_PLUGIN = `{
  "schema": 1,
  "id": "my-agent",
  "name": "My Agent",
  "kind": "cli",
  "protocols": ["openai"],
  "bins": ["my-agent"],
  "run_args": ["run", "--prompt", "{prompt}", "--model", "{model}"],
  "env": {
    "OPENAI_BASE_URL": "{base_url}",
    "OPENAI_API_KEY": "{api_key}"
  },
  "output": "text"
}`

function coreUsable(core: AgentCore): boolean {
  if (!core.available) return false
  if (core.kind === 'kb') return true
  return (core.compatible_model_count ?? 0) > 0
}

function coreHint(core: AgentCore): string {
  if (!core.installed) return core.unavailable_reason || '未安装'
  if (!core.enabled) return '已停用'
  if (!coreUsable(core)) return '还没有协议兼容的模型，请先在「模型设置」里添加'
  return core.description
}

export default function AgentPage() {
  const [cores, setCores] = useState<AgentCore[]>([])
  const [coreErrors, setCoreErrors] = useState<AgentPluginError[]>([])
  const [pluginDir, setPluginDir] = useState('')
  const [sessions, setSessions] = useState<AgentSession[]>([])
  const [active, setActive] = useState<AgentSessionDetail | null>(null)
  const [models, setModels] = useState<AgentModelOption[]>([])
  const [kbs, setKbs] = useState<KnowledgeBase[]>([])
  const [boot, setBoot] = useState(true)
  const [loadError, setLoadError] = useState<unknown>(null)
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [live, setLive] = useState<LiveRun | null>(null)
  const [pluginsOpen, setPluginsOpen] = useState(false)
  const [busyPlugin, setBusyPlugin] = useState('')
  const [renaming, setRenaming] = useState(false)
  const [renameText, setRenameText] = useState('')
  const [workspaceOpen, setWorkspaceOpen] = useState(false)
  const [workspaceText, setWorkspaceText] = useState('')
  const [deleteTarget, setDeleteTarget] = useState<AgentSession | null>(null)
  const [notice, setNotice] = useState('')

  const scrollRef = useRef<HTMLDivElement>(null)
  const pollToken = useRef(0)
  const activeId = useRef<number | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const availableCores = useMemo(() => cores.filter(coreUsable), [cores])
  const preferredCore = useMemo(
    () => availableCores.find((c) => c.kind === 'kb') ?? availableCores[0] ?? null,
    [availableCores],
  )
  const activeCore = useMemo(
    () => cores.find((c) => c.id === active?.core_id) ?? active?.core ?? null,
    [cores, active],
  )
  const compatibleModels = useMemo(
    () => models.filter((m) => m.compatible || m.id === active?.model_config_id),
    [models, active],
  )
  const isBusy = Boolean(live) || Boolean(active?.running)

  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(''), 4000)
    return () => window.clearTimeout(timer)
  }, [notice])

  useEffect(() => {
    let alive = true
    void (async () => {
      try {
        const [coreData, sessionData, kbData] = await Promise.all([
          agentApi.cores(),
          agentApi.sessions(),
          kbApi.list(),
        ])
        if (!alive) return
        setCores(coreData.items)
        setCoreErrors(coreData.errors)
        setPluginDir(coreData.plugin_dir)
        setSessions(sessionData.items)
        setKbs(kbData.items)
        if (sessionData.items[0]) {
          await openSession(sessionData.items[0].id, alive)
        }
      } catch (err) {
        if (alive) setLoadError(err)
      } finally {
        if (alive) setBoot(false)
      }
    })()
    return () => {
      alive = false
      pollToken.current += 1
    }
    // 只在页面首次进入时拉一次；后续由显式操作刷新。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!active) return
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [active, live?.text, live?.logs.length])

  async function refreshCores(): Promise<AgentCore[]> {
    const data = await agentApi.cores()
    setCores(data.items)
    setCoreErrors(data.errors)
    setPluginDir(data.plugin_dir)
    return data.items
  }

  async function refreshSessions(): Promise<AgentSession[]> {
    const data = await agentApi.sessions()
    setSessions(data.items)
    return data.items
  }

  async function openSession(id: number, alive = true) {
    pollToken.current += 1
    const token = pollToken.current
    setLive(null)
    const detail = await agentApi.session(id)
    if (!alive || token !== pollToken.current) return
    setActive(detail)
    activeId.current = id
    setWorkspaceText(detail.workspace)
    try {
      const data = await agentApi.models(detail.core_id)
      if (token === pollToken.current) setModels(data.items)
    } catch {
      if (token === pollToken.current) setModels([])
    }
    const pending = [...detail.messages]
      .reverse()
      .find((m) => m.role === 'assistant' && m.status === 'pending')
    if (pending) {
      startPolling(id, pending.id)
    }
  }

  function startPolling(sessionId: number, runId: number) {
    const token = ++pollToken.current
    let after = 0
    const tick = async () => {
      if (token !== pollToken.current) return
      try {
        const snap = await agentApi.events(runId, after)
        if (token !== pollToken.current) return
        after = snap.next
        setLive((prev) => {
          const base: LiveRun = prev && prev.runId === runId
            ? prev
            : {
                runId, sessionId, text: '', logs: [], evidence: [],
                stage: '', status: 'running', error: '',
              }
          let text = base.text
          const logs = [...base.logs]
          let evidence = [...base.evidence]
          let stage = base.stage
          let status = base.status
          let error = base.error
          for (const ev of snap.events) {
            const data = ev.data as Record<string, unknown>
            if (ev.type === 'text') {
              const delta = typeof data.text === 'string' ? data.text : ''
              if (delta) text += delta
            } else if (ev.type === 'log') {
              const line = typeof data.text === 'string' ? data.text : ''
              if (line) logs.push(line)
            } else if (ev.type === 'status') {
              stage = typeof data.text === 'string' ? data.text : stage
            } else if (ev.type === 'evidence') {
              evidence = Array.isArray(data.items) ? (data.items as EvidenceItem[]) : evidence
            } else if (ev.type === 'error') {
              status = 'failed'
              error = typeof data.message === 'string' ? data.message : '执行失败'
            } else if (ev.type === 'stopped') {
              status = 'stopped'
            } else if (ev.type === 'done') {
              status = 'ok'
            }
          }
          return {
            ...base, text, logs: logs.slice(-200), evidence, stage,
            status: snap.status === 'running' ? status : snap.status,
            error: snap.error || error,
          }
        })
        if (snap.status === 'running') {
          window.setTimeout(() => void tick(), 350)
          return
        }
        const detail = await agentApi.session(sessionId)
        if (token !== pollToken.current) return
        setActive(detail)
        setLive(null)
        void refreshSessions()
      } catch (err) {
        if (token !== pollToken.current) return
        setLive((prev) => (prev ? { ...prev, status: 'failed', error: errorText(err) } : prev))
      }
    }
    void tick()
  }

  async function createSession(core?: AgentCore | null, modelId?: number | null) {
    const chosen = core ?? preferredCore
    if (!chosen) {
      setPluginsOpen(true)
      setNotice('没有可用的 Agent 核心：请先安装 CLI，或在核心管理里启用插件。')
      return
    }
    if (!coreUsable(chosen)) {
      setNotice(
        chosen.installed
          ? `${chosen.name} 还没有协议兼容的模型，请先在「模型设置」里添加。`
          : `${chosen.name} 未安装：${chosen.unavailable_reason}`,
      )
      return
    }
    try {
      const created = await agentApi.createSession({
        core_id: chosen.id,
        model_config_id: modelId ?? chosen.default_model_id ?? null,
        kb_ids: chosen.kind === 'kb' ? kbs.map((k) => k.id) : [],
      })
      await refreshSessions()
      await openSession(created.id)
      setDraft('')
      window.setTimeout(() => textareaRef.current?.focus(), 60)
    } catch (err) {
      setNotice(errorText(err))
    }
  }

  async function send() {
    const content = draft.trim()
    if (!content || sending || isBusy) return
    let sessionId = active?.id ?? null
    if (sessionId === null) {
      const chosen = preferredCore
      if (!chosen) {
        setPluginsOpen(true)
        return
      }
      try {
        const created = await agentApi.createSession({
          core_id: chosen.id,
          model_config_id: chosen.default_model_id ?? null,
          kb_ids: chosen.kind === 'kb' ? kbs.map((k) => k.id) : [],
        })
        sessionId = created.id
        await openSession(created.id)
      } catch (err) {
        setNotice(errorText(err))
        return
      }
    }
    if (sessionId === null) return
    setSending(true)
    try {
      const res = await agentApi.send(sessionId, content)
      setDraft('')
      setActive((prev) => {
        if (!prev || prev.id !== sessionId) return prev
        return {
          ...prev,
          title: res.session.title,
          status: 'running',
          message_count: prev.message_count + 2,
          messages: [...prev.messages, res.user_message, res.assistant_message],
        }
      })
      setSessions((prev) => [res.session, ...prev.filter((s) => s.id !== res.session.id)])
      setLive({
        runId: res.run_id,
        sessionId,
        text: '',
        logs: [],
        evidence: [],
        stage: '正在启动…',
        status: 'running',
        error: '',
      })
      startPolling(sessionId, res.run_id)
      window.setTimeout(() => textareaRef.current?.focus(), 60)
    } catch (err) {
      setNotice(errorText(err))
    } finally {
      setSending(false)
    }
  }

  async function stopRun() {
    if (!live) return
    try {
      await agentApi.stop(live.runId)
      setNotice('已发送停止请求。')
    } catch (err) {
      setNotice(errorText(err))
    }
  }

  async function changeCore(coreId: string) {
    if (!active || isBusy) return
    try {
      await agentApi.updateSession(active.id, { core_id: coreId })
      await openSession(active.id)
      await refreshSessions()
    } catch (err) {
      setNotice(errorText(err))
    }
  }

  async function changeModel(value: string) {
    if (!active || isBusy) return
    try {
      const updated = await agentApi.updateSession(active.id, {
        model_config_id: value ? Number(value) : null,
      })
      setActive((prev) => (prev ? {
        ...prev,
        model_config_id: updated.model_config_id,
        model_label: updated.model_label,
      } : prev))
      setSessions((prev) => prev.map((s) => (s.id === updated.id ? updated : s)))
    } catch (err) {
      setNotice(errorText(err))
    }
  }

  async function toggleKb(kbId: number) {
    if (!active || isBusy) return
    const next = active.kb_ids.includes(kbId)
      ? active.kb_ids.filter((id) => id !== kbId)
      : [...active.kb_ids, kbId]
    try {
      const updated = await agentApi.updateSession(active.id, { kb_ids: next })
      setActive((prev) => (prev ? { ...prev, kb_ids: updated.kb_ids } : prev))
      setSessions((prev) => prev.map((s) => (s.id === updated.id ? updated : s)))
    } catch (err) {
      setNotice(errorText(err))
    }
  }

  async function saveTitle() {
    if (!active) return
    try {
      const updated = await agentApi.updateSession(active.id, { title: renameText.trim() })
      setActive((prev) => (prev ? { ...prev, title: updated.title } : prev))
      setSessions((prev) => prev.map((s) => (s.id === updated.id ? updated : s)))
      setRenaming(false)
    } catch (err) {
      setNotice(errorText(err))
    }
  }

  async function saveWorkspace() {
    if (!active) return
    try {
      const updated = await agentApi.updateSession(active.id, { workspace: workspaceText.trim() })
      setActive((prev) => (prev ? { ...prev, workspace: updated.workspace } : prev))
      setWorkspaceOpen(false)
    } catch (err) {
      setNotice(errorText(err))
    }
  }

  async function removeSession() {
    if (!deleteTarget) return
    try {
      await agentApi.deleteSession(deleteTarget.id)
      const remaining = await refreshSessions()
      setDeleteTarget(null)
      if (activeId.current === deleteTarget.id) {
        pollToken.current += 1
        setActive(null)
        setLive(null)
        activeId.current = null
        if (remaining[0]) await openSession(remaining[0].id)
      }
    } catch (err) {
      setNotice(errorText(err))
    }
  }

  async function togglePlugin(core: AgentCore) {
    setBusyPlugin(core.id)
    try {
      await agentApi.togglePlugin(core.id, !core.enabled)
      await refreshCores()
    } catch (err) {
      setNotice(errorText(err))
    } finally {
      setBusyPlugin('')
    }
  }

  async function rescanPlugins() {
    setBusyPlugin('__rescan__')
    try {
      await refreshCores()
      setNotice('已重新扫描核心与插件。')
    } catch (err) {
      setNotice(errorText(err))
    } finally {
      setBusyPlugin('')
    }
  }

  async function copyPluginExample() {
    try {
      await navigator.clipboard.writeText(EXAMPLE_PLUGIN)
      setNotice('示例插件 JSON 已复制。')
    } catch {
      setNotice('复制失败，请手动选中示例内容。')
    }
  }

  if (boot) {
    return <div className="state-block"><LoadingBlock text="正在加载 Agent 核心…" /></div>
  }
  if (loadError) {
    return (
      <div className="state-block">
        <ErrorState error={loadError} onRetry={() => window.location.reload()} />
      </div>
    )
  }

  const visibleMessages = (active?.messages ?? []).filter(
    (m) => !(live && m.role === 'assistant' && m.status === 'pending'),
  )
  const kbNames = active ? active.kb_ids
    .map((id) => kbs.find((k) => k.id === id)?.name ?? `#${id}`) : []

  return (
    <div className="chat-shell agent-shell">
      <aside className="chat-sidebar agent-rail">
        <div className="chat-sidebar-head">
          <span className="agent-brand"><IconAgent size={16} /> 个人 Agent</span>
          <span className="spacer" />
          <button
            type="button"
            className="icon-btn"
            title="核心与插件"
            aria-label="核心与插件"
            onClick={() => setPluginsOpen(true)}
          >
            <IconPlug size={16} />
          </button>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            onClick={() => void createSession()}
            disabled={!preferredCore}
          >
            <IconPlus size={13} /> 新建
          </button>
        </div>

        <div className="agent-rail-status">
          {activeCore ? (
            <>
              <div className="row">
                <span className="agent-core-dot" data-kind={activeCore.kind} />
                <strong className="truncate">{activeCore.name}</strong>
                <span className="spacer" />
                {activeCore.kind === 'cli'
                  ? <Badge tone="info">{activeCore.version || '已安装'}</Badge>
                  : <Badge tone="success">内置</Badge>}
              </div>
              <div className="tiny muted truncate">
                {active?.model_label || '跟随应用默认模型'}
              </div>
            </>
          ) : (
            <div className="tiny muted">从下方选择核心开始，或新建一个会话。</div>
          )}
        </div>

        <div className="conv-list">
          {sessions.length === 0 ? (
            <div className="agent-session-empty">还没有会话</div>
          ) : sessions.map((s) => (
            <div key={s.id} className={`conv-item ${active?.id === s.id ? 'active' : ''}`}>
              <button
                type="button"
                className="conv-title agent-session-title"
                onClick={() => void openSession(s.id)}
                title={`${s.core_name} · ${s.model_label || '默认模型'}`}
              >
                <span className="truncate">{s.title}</span>
                <span className="agent-session-sub">{s.core_name}</span>
              </button>
              <span className="conv-actions">
                <button
                  type="button"
                  title="删除会话"
                  aria-label={`删除会话 ${s.title}`}
                  onClick={() => setDeleteTarget(s)}
                >
                  ×
                </button>
              </span>
            </div>
          ))}
        </div>

        <div className="agent-rail-foot">
          <button type="button" className="btn btn-ghost btn-sm full" onClick={() => setPluginsOpen(true)}>
            <IconPlug size={14} /> 核心与插件
          </button>
          <div className="tiny muted agent-foot-hint">
            CLI 核心以本机命令运行，仅在管理员账号下可用。
          </div>
        </div>
      </aside>

      <section className="chat-main">
        {notice ? (
          <div className="agent-notice" role="status">
            <IconAlert size={14} />
            <span>{notice}</span>
            <button type="button" className="btn-link" onClick={() => setNotice('')}>关闭</button>
          </div>
        ) : null}

        {!active ? (
          <div className="chat-scroll">
            <div className="agent-onboarding">
              <div className="agent-hero">
                <div className="agent-hero-icon"><IconAgent size={26} /></div>
                <h1>本地个人 Agent</h1>
                <p>
                  选一个核心开始：内置知识库助手直接检索本应用的资料；
                  Claude Code、Codex 等开源 CLI 复用你已经配置好的模型，随时换核心。
                </p>
                <div className="row row-wrap" style={{ justifyContent: 'center' }}>
                  <Button variant="primary" onClick={() => void createSession()} disabled={!preferredCore}>
                    <IconPlus size={14} /> 用 {preferredCore?.name ?? '可用核心'} 开始
                  </Button>
                  <Button variant="ghost" onClick={() => setPluginsOpen(true)}>
                    <IconPlug size={14} /> 核心与插件
                  </Button>
                </div>
              </div>

              <div className="agent-core-grid">
                {cores.map((core) => (
                  <button
                    key={core.id}
                    type="button"
                    className={`agent-core-card ${coreUsable(core) ? '' : 'disabled'}`}
                    onClick={() => void createSession(core)}
                    disabled={!coreUsable(core)}
                    title={coreHint(core)}
                  >
                    <div className="row">
                      <span className="agent-core-icon" data-kind={core.kind}>
                        {core.kind === 'kb' ? <IconAgent size={16} /> : <IconTerminal size={16} />}
                      </span>
                      <strong>{core.name}</strong>
                      <span className="spacer" />
                      {core.builtin ? <Badge tone="muted">内置</Badge> : <Badge tone="info">插件</Badge>}
                    </div>
                    <div className="agent-core-desc">{core.description}</div>
                    <div className="agent-core-meta">
                      {core.kind === 'cli'
                        ? (core.installed
                          ? <span className="ok">{core.version || '已安装'}</span>
                          : <span className="muted">未安装 · {core.bins.join(' / ')}</span>)
                        : <span className="ok">开箱即用</span>}
                      <span className="spacer" />
                      {core.kind === 'cli' && core.installed && !coreUsable(core)
                        ? <span className="warn-text">无兼容模型</span>
                        : <span className="muted">{core.protocol_labels.join(' / ') || '应用内检索'}</span>}
                    </div>
                  </button>
                ))}
              </div>
              {cores.length === 0 ? (
                <EmptyState
                  title="还没有可用的核心"
                  hint="先在本机安装 Claude Code / Codex 等 CLI，或在核心管理里放置插件清单。"
                  action={<Button onClick={() => setPluginsOpen(true)}>打开核心管理</Button>}
                />
              ) : null}
            </div>
          </div>
        ) : (
          <>
            <div className="chat-toolbar agent-toolbar">
              <button
                type="button"
                className="agent-title-btn"
                onClick={() => {
                  setRenameText(active.title)
                  setRenaming(true)
                }}
                title="重命名会话"
              >
                <strong>{active.title}</strong>
              </button>
              <span className="agent-toolbar-divider" />
              <label className="agent-inline-select">
                <span className="tiny muted">核心</span>
                <Select
                  value={active.core_id}
                  onChange={(e) => void changeCore(e.target.value)}
                  disabled={isBusy}
                  aria-label="切换 Agent 核心"
                >
                  {cores.map((core) => (
                    <option
                      key={core.id}
                      value={core.id}
                      disabled={!coreUsable(core) && core.id !== active.core_id}
                    >
                      {core.name}{coreUsable(core) ? '' : '（不可用）'}
                    </option>
                  ))}
                </Select>
              </label>
              <label className="agent-inline-select">
                <span className="tiny muted">模型</span>
                <Select
                  value={active.model_config_id ?? ''}
                  onChange={(e) => void changeModel(e.target.value)}
                  disabled={isBusy || compatibleModels.length === 0}
                  aria-label="选择模型"
                >
                  {activeCore?.kind === 'kb' ? <option value="">跟随应用默认</option> : null}
                  {compatibleModels.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.display}{m.compatible ? '' : '（协议不兼容）'}
                    </option>
                  ))}
                  {compatibleModels.length === 0 ? <option value="">没有兼容模型</option> : null}
                </Select>
              </label>
              {activeCore?.kind === 'kb' ? (
                <details className="agent-kb-picker">
                  <summary className="btn btn-ghost btn-sm">
                    知识库 {active.kb_ids.length} 个
                  </summary>
                  <div className="agent-kb-pop">
                    {kbs.length === 0 ? <div className="tiny muted">还没有知识库</div> : kbs.map((kb) => (
                      <label key={kb.id} className="agent-kb-option">
                        <input
                          type="checkbox"
                          checked={active.kb_ids.includes(kb.id)}
                          disabled={isBusy}
                          onChange={() => void toggleKb(kb.id)}
                        />
                        <span className="truncate">{kb.name}</span>
                      </label>
                    ))}
                  </div>
                </details>
              ) : null}
              {activeCore?.kind === 'cli' ? (
                <button
                  type="button"
                  className="agent-workspace"
                  onClick={() => setWorkspaceOpen(true)}
                  title={active.workspace}
                >
                  <IconTerminal size={13} />
                  <span className="truncate">{active.workspace}</span>
                </button>
              ) : null}
              <span className="spacer" />
              {live ? (
                <Button variant="ghost" size="sm" onClick={() => void stopRun()}>
                  <IconStop size={13} /> 停止
                </Button>
              ) : null}
              <Button variant="ghost" size="sm" onClick={() => setPluginsOpen(true)}>
                <IconPlug size={13} /> 核心
              </Button>
            </div>

            <div className="chat-scroll" ref={scrollRef}>
              <div className="chat-thread">
                {visibleMessages.length === 0 && !live ? (
                  <div className="chat-empty agent-thread-empty">
                    <div className="chat-empty-icon"><IconAgent size={22} /></div>
                    <div className="chat-empty-title">开始和 {activeCore?.name ?? 'Agent'} 对话</div>
                    <div className="chat-empty-hint">
                      {activeCore?.kind === 'kb'
                        ? `会检索 ${kbNames.length} 个知识库并给出引用；资料不足时会说明缺什么。`
                        : '这条消息会交给本机 CLI 执行，模型与密钥由本应用注入，不会写进你的 CLI 配置文件。'}
                    </div>
                  </div>
                ) : null}

                {visibleMessages.map((m) => (
                  <AgentMessageBubble key={m.id} message={m} />
                ))}

                {live ? <LiveBubble live={live} core={activeCore} onStop={() => void stopRun()} /> : null}
              </div>
            </div>

            <div className="chat-composer">
              <div className="composer-inner">
                <textarea
                  ref={textareaRef}
                  className="composer-input"
                  rows={1}
                  value={draft}
                  placeholder={isBusy ? '正在执行，可以点上方「停止」…' : '问点什么，或让 Agent 动手做一件事…'}
                  disabled={isBusy}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                      e.preventDefault()
                      void send()
                    }
                  }}
                />
                <Button
                  variant="primary"
                  onClick={() => void send()}
                  disabled={!draft.trim() || sending || isBusy}
                  loading={sending}
                >
                  发送
                </Button>
              </div>
              <div className="composer-hint">
                <span>Enter 发送 · Shift+Enter 换行</span>
                <span>核心：{activeCore?.name ?? '—'}</span>
                <span>模型：{active.model_label || '应用默认'}</span>
                {activeCore?.kind === 'kb' ? <span>知识库：{kbNames.length} 个</span> : null}
              </div>
            </div>
          </>
        )}
      </section>

      <Modal
        open={pluginsOpen}
        title="Agent 核心与插件"
        width={760}
        onClose={() => setPluginsOpen(false)}
        footer={
          <>
            <span className="tiny muted" style={{ marginRight: 'auto' }}>
              插件只描述「怎么启动 CLI」，应用不会加载第三方代码。
            </span>
            <Button variant="ghost" onClick={() => setPluginsOpen(false)}>关闭</Button>
            <Button
              onClick={() => void rescanPlugins()}
              loading={busyPlugin === '__rescan__'}
            >
              <IconRefresh size={14} /> 重新扫描
            </Button>
          </>
        }
      >
        <div className="agent-plugin-body">
          {coreErrors.length > 0 ? (
            <div className="agent-plugin-errors">
              <strong>有 {coreErrors.length} 个清单未能加载</strong>
              {coreErrors.map((e) => (
                <div key={e.file} className="tiny">
                  <code>{e.file}</code>：{e.error}
                </div>
              ))}
            </div>
          ) : null}

          <div className="agent-plugin-list">
            {cores.map((core) => (
              <div key={core.id} className="agent-plugin-row">
                <span className="agent-core-icon" data-kind={core.kind}>
                  {core.kind === 'kb' ? <IconAgent size={16} /> : <IconTerminal size={16} />}
                </span>
                <div className="agent-plugin-main">
                  <div className="row">
                    <strong>{core.name}</strong>
                    {core.builtin ? <Badge tone="muted">内置</Badge> : <Badge tone="info">插件</Badge>}
                    {core.installed
                      ? <Badge tone="success">已安装</Badge>
                      : <Badge tone="warn">未安装</Badge>}
                    {core.kind === 'cli' && core.installed && !coreUsable(core)
                      ? <Badge tone="warn">无兼容模型</Badge>
                      : null}
                    {!core.enabled ? <Badge tone="danger">已停用</Badge> : null}
                  </div>
                  <div className="tiny muted agent-plugin-desc">{core.description}</div>
                  <div className="tiny muted">
                    {core.kind === 'cli'
                      ? `${core.bins.join(' / ')}${core.version ? ` · ${core.version}` : ''}${core.path ? ` · ${core.path}` : ''}`
                      : '内置 RAG 核心，无需安装'}
                  </div>
                  {core.protocol_labels.length > 0 ? (
                    <div className="tiny muted">协议：{core.protocol_labels.join(' / ')}</div>
                  ) : null}
                </div>
                <Button
                  variant={core.enabled ? 'ghost' : 'primary'}
                  size="sm"
                  loading={busyPlugin === core.id}
                  disabled={core.id === 'kb'}
                  title={core.id === 'kb' ? '内置知识库核心不能停用' : undefined}
                  onClick={() => void togglePlugin(core)}
                >
                  {core.id === 'kb' ? '内置' : core.enabled ? '停用' : '启用'}
                </Button>
              </div>
            ))}
          </div>

          <div className="divider" />
          <Field label="插件目录" hint="把 JSON 清单放到这里，点「重新扫描」即可出现。">
            <div className="row">
              <TextInput readOnly value={pluginDir} onFocus={(e) => e.currentTarget.select()} />
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  void navigator.clipboard
                    ?.writeText(pluginDir)
                    .catch(() => setNotice('复制失败，请手动选择路径'))
                }}
              >
                复制路径
              </Button>
            </div>
          </Field>
          <div className="row" style={{ justifyContent: 'space-between' }}>
            <span className="tiny muted">示例清单（可复制后改）</span>
            <Button variant="ghost" size="sm" onClick={() => void copyPluginExample()}>复制示例</Button>
          </div>
          <pre className="agent-plugin-example">{EXAMPLE_PLUGIN}</pre>
          <div className="tiny muted">
            占位符：<code>{'{prompt}'}</code> 提示词、<code>{'{model}'}</code> 模型 id、
            <code>{'{base_url}'}</code> 供应商地址、<code>{'{api_key}'}</code> 密钥、
            <code>{'{workspace}'}</code> 工作目录。完整字段见仓库 <code>docs/AGENT_PLUGINS.md</code>。
          </div>
        </div>
      </Modal>

      <Modal
        open={renaming}
        title="重命名会话"
        width={420}
        onClose={() => setRenaming(false)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setRenaming(false)}>取消</Button>
            <Button variant="primary" onClick={() => void saveTitle()}>保存</Button>
          </>
        }
      >
        <Field label="标题">
          <TextInput
            value={renameText}
            maxLength={120}
            onChange={(e) => setRenameText(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') void saveTitle() }}
            autoFocus
          />
        </Field>
      </Modal>

      <Modal
        open={workspaceOpen}
        title="CLI 工作目录"
        width={520}
        onClose={() => setWorkspaceOpen(false)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setWorkspaceOpen(false)}>取消</Button>
            <Button variant="primary" onClick={() => void saveWorkspace()}>保存</Button>
          </>
        }
      >
        <Field
          label="目录绝对路径"
          hint="Agent 能读写这个目录；建议指到单独的项目文件夹，不要用仓库根目录。"
        >
          <TextInput
            value={workspaceText}
            onChange={(e) => setWorkspaceText(e.target.value)}
            placeholder="例如 C:\\Users\\me\\projects\\demo"
          />
        </Field>
      </Modal>

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title="删除会话"
        message={<>确定删除「{deleteTarget?.title}」及其全部消息？此操作不可恢复。</>}
        confirmText="删除"
        danger
        onConfirm={() => void removeSession()}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  )
}

function AgentMessageBubble({ message }: { message: AgentMessage }) {
  if (message.role === 'system') {
    return (
      <div className="agent-system-line">
        <span>{message.content}</span>
      </div>
    )
  }
  const isUser = message.role === 'user'
  const usage = (message.meta?.usage ?? null) as UsageInfo | null
  const metaError = typeof message.meta?.error === 'string' ? message.meta.error : ''
  const failed = message.status === 'failed'
  const stopped = message.status === 'stopped'
  if (!isUser && message.status === 'pending') {
    return (
      <div className="msg msg-assistant">
        <div className="msg-avatar"><IconAgent size={15} /></div>
        <div className="msg-main">
          <div className="msg-bubble agent-live">
            <div className="agent-live-wait"><Spinner size={14} /><span>正在执行…</span></div>
          </div>
        </div>
      </div>
    )
  }
  return (
    <div className={`msg ${isUser ? 'msg-user' : 'msg-assistant'}`}>
      <div className="msg-avatar">
        {isUser ? '我' : <IconAgent size={15} />}
      </div>
      <div className="msg-main">
        <div className={`msg-bubble ${failed || stopped ? 'msg-failed' : ''}`}>
          {isUser ? (
            <div style={{ whiteSpace: 'pre-wrap' }}>{message.content}</div>
          ) : (
            <Markdown content={message.content || (failed ? metaError || '这次执行没有完成。' : '')} />
          )}
        </div>
        <div className="msg-meta">
          <span>{formatTime(message.created_at)}</span>
          {message.core_name ? <span>{message.core_name}</span> : null}
          {message.model_label ? <span>{message.model_label}</span> : null}
          {message.latency_ms ? <span>{formatMs(message.latency_ms)}</span> : null}
          {stopped ? <span>已停止</span> : null}
          {failed ? <span className="danger">未完成</span> : null}
        </div>
        {!isUser && message.evidence.length > 0 ? (
          <details className="agent-evidence">
            <summary className="evidence-toggle">证据 {message.evidence.length} 条</summary>
            <EvidencePanel evidence={message.evidence} />
          </details>
        ) : null}
        {!isUser && usage ? <UsageLine usage={usage} /> : null}
      </div>
    </div>
  )
}

function LiveBubble({
  live,
  core,
  onStop,
}: {
  live: LiveRun
  core: AgentCore | null
  onStop: () => void
}) {
  const failed = live.status === 'failed'
  const stopped = live.status === 'stopped'
  return (
    <div className="msg msg-assistant">
      <div className="msg-avatar"><IconAgent size={15} /></div>
      <div className="msg-main">
        <div className={`msg-bubble agent-live ${failed ? 'msg-failed' : ''}`}>
          {live.text ? <Markdown content={live.text} /> : (
            <div className="agent-live-wait">
              <Spinner size={14} />
              <span>{live.stage || '正在执行…'}</span>
            </div>
          )}
          {live.text && live.status === 'running' ? <span className="agent-caret" /> : null}
          {failed || stopped ? (
            <div className="agent-live-error">
              {stopped ? '已停止。' : live.error || '执行失败。'}
            </div>
          ) : null}
        </div>
        <div className="msg-meta">
          <span>{core?.name ?? 'Agent'}</span>
          {live.status === 'running' ? <span>{live.stage || '执行中'}</span> : null}
          {live.status === 'running' ? (
            <button type="button" className="btn-link" onClick={onStop}>停止</button>
          ) : null}
        </div>
        {live.logs.length > 0 ? (
          <details className="agent-logs">
            <summary className="evidence-toggle">运行日志 {live.logs.length} 行</summary>
            <pre>{live.logs.join('\n')}</pre>
          </details>
        ) : null}
        {live.evidence.length > 0 ? (
          <details className="agent-evidence" open>
            <summary className="evidence-toggle">证据 {live.evidence.length} 条</summary>
            <EvidencePanel evidence={live.evidence} />
          </details>
        ) : null}
      </div>
    </div>
  )
}

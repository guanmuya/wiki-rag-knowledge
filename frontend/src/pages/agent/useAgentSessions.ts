import { useEffect, useMemo, useRef, useState } from 'react'
import { agentApi, kbApi } from '../../api'
import type {
  AgentCore,
  AgentCoreList,
  AgentModelOption,
  AgentSession,
  AgentSessionDetail,
  EvidenceItem,
  KnowledgeBase,
} from '../../types'
import { errorText } from '../../utils'
import { coreUsable, type LiveRun } from './constants'

export interface AgentSessionsOptions {
  cores: AgentCore[]
  applyCoreList: (data: AgentCoreList) => void
  notify: (text: string) => void
  setPluginsOpen: (open: boolean) => void
}

/** 会话、当前会话详情、模型/知识库选项，以及一次运行的事件轮询。 */
export function useAgentSessions({
  cores,
  applyCoreList,
  notify,
  setPluginsOpen,
}: AgentSessionsOptions) {
  const [sessions, setSessions] = useState<AgentSession[]>([])
  const [active, setActive] = useState<AgentSessionDetail | null>(null)
  const [models, setModels] = useState<AgentModelOption[]>([])
  const [kbs, setKbs] = useState<KnowledgeBase[]>([])
  const [boot, setBoot] = useState(true)
  const [loadError, setLoadError] = useState<unknown>(null)
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [live, setLive] = useState<LiveRun | null>(null)
  const [renaming, setRenaming] = useState(false)
  const [renameText, setRenameText] = useState('')
  const [workspaceOpen, setWorkspaceOpen] = useState(false)
  const [workspaceText, setWorkspaceText] = useState('')
  const [deleteTarget, setDeleteTarget] = useState<AgentSession | null>(null)

  const scrollRef = useRef<HTMLDivElement>(null)
  const pollToken = useRef(0)
  const activeId = useRef<number | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const availableCores = useMemo(() => cores.filter(coreUsable), [cores])
  const preferredCore = useMemo(
    () => availableCores.find((c) => c.kind === 'kb') ?? availableCores[0] ?? null,
    [availableCores],
  )
  const isBusy = Boolean(live) || Boolean(active?.running)

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
        applyCoreList(coreData)
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
      notify('没有可用的 Agent 核心：请先安装 CLI，或在核心管理里启用插件。')
      return
    }
    if (!coreUsable(chosen)) {
      notify(
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
      notify(errorText(err))
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
        notify(errorText(err))
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
      notify(errorText(err))
    } finally {
      setSending(false)
    }
  }

  async function stopRun() {
    if (!live) return
    try {
      await agentApi.stop(live.runId)
      notify('已发送停止请求。')
    } catch (err) {
      notify(errorText(err))
    }
  }

  async function changeCore(coreId: string) {
    if (!active || isBusy) return
    try {
      await agentApi.updateSession(active.id, { core_id: coreId })
      await openSession(active.id)
      await refreshSessions()
    } catch (err) {
      notify(errorText(err))
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
      notify(errorText(err))
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
      notify(errorText(err))
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
      notify(errorText(err))
    }
  }

  async function saveWorkspace() {
    if (!active) return
    try {
      const updated = await agentApi.updateSession(active.id, { workspace: workspaceText.trim() })
      setActive((prev) => (prev ? { ...prev, workspace: updated.workspace } : prev))
      setWorkspaceOpen(false)
    } catch (err) {
      notify(errorText(err))
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
      notify(errorText(err))
    }
  }

  return {
    sessions,
    active,
    models,
    kbs,
    boot,
    loadError,
    draft,
    setDraft,
    sending,
    live,
    renaming,
    setRenaming,
    renameText,
    setRenameText,
    workspaceOpen,
    setWorkspaceOpen,
    workspaceText,
    setWorkspaceText,
    deleteTarget,
    setDeleteTarget,
    preferredCore,
    isBusy,
    scrollRef,
    textareaRef,
    openSession,
    createSession,
    send,
    stopRun,
    changeCore,
    changeModel,
    toggleKb,
    saveTitle,
    saveWorkspace,
    removeSession,
  }
}

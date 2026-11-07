import { Fragment, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { useAuth, useToast } from '../auth'
import { ApiError, chatApi, getToken, kbApi } from '../api'
import type {
  AskResponse,
  AskStreamEvent,
  ChatMessage,
  ChatModelOption,
  Conversation,
  ConversationDetail,
  KnowledgeBase,
  PipelineStep,
  ThinkingStream,
} from '../types'
import { errorText, formatMs, formatTime, parseApiTime } from '../utils'
import { Button, ErrorState, Modal, Select, SkeletonLines, Spinner, TextInput } from '../components/ui'
import { Markdown } from '../components/Markdown'
import { MessageInsights } from '../components/Evidence'
import { IconChevronDown, IconChevronRight, IconClose, IconMenu, IconMessage } from '../components/icons'

// Keep only this page's outstanding requests across an ordinary route unmount.
// The server remains authoritative after a reload or in another browser tab.
type AskJob = {
  key: number
  userId: number | null
  session: string | null
  visit: number
  intent: number
  targetId: number | null
  text: string
  kbIds: number[]
  baselineId: number
  initialDetail: ConversationDetail | null
  phase: 'creating' | 'asking' | 'success' | 'error'
  response?: AskResponse
  /** 流式问答期间的思考步骤与答案增量；完成后清空，由最终消息接管。 */
  stream?: ThinkingStream
  error?: string
  ambiguous?: boolean
  matchingTurnSeen?: boolean
}
type ChatJobEvent = {
  kind: 'change' | 'list' | 'settled'
  userId: number | null
  session: string | null
  job?: AskJob
}
const MAX_CHAT_JOBS = 32
const askJobs = new Map<number, AskJob>()
const visibleSuccessJobs = new Set<number>()
const jobListeners = new Set<(event: ChatJobEvent) => void>()
let nextJobKey = 0
let nextVisitKey = 0

function publishJob(kind: ChatJobEvent['kind'], job: AskJob) {
  jobListeners.forEach((listener) => listener({ kind, userId: job.userId, session: job.session, job }))
}

function publishList(userId: number | null, session: string | null) {
  jobListeners.forEach((listener) => listener({ kind: 'list', userId, session }))
}

function forgetJob(job: AskJob) {
  if (askJobs.get(job.key) !== job) return
  askJobs.delete(job.key)
  publishJob('change', job)
}

function retainSettledJob(job: AskJob) {
  // Retain a visible successful fallback; clean up abandoned records eventually.
  const expire = () => {
    if (askJobs.get(job.key) !== job) return
    if (job.phase === 'success' && visibleSuccessJobs.has(job.key)) {
      window.setTimeout(expire, 60 * 1000)
    } else if (job.phase === 'success') {
      forgetJob(job)
    }
  }
  window.setTimeout(expire, 5 * 60 * 1000)
}

function jobMatchesSession(job: AskJob, userId: number | null) {
  return job.userId === userId && (job.session === getToken() || job.phase === 'error')
}

function serverHasQuestion(messages: ChatMessage[], job: AskJob) {
  return messages.some((message, index) =>
    message.id > job.baselineId &&
    message.role === 'user' &&
    message.content === job.text &&
    Boolean(message.kb_ids?.length) &&
    message.kb_ids?.every((id) => job.kbIds.includes(id)) &&
    messages[index + 1]?.role === 'assistant',
  )
}

function withJobAnswer(messages: ChatMessage[], job: AskJob): ChatMessage[] {
  const res = job.response
  if (!res) return messages
  const assistant: ChatMessage = {
    ...res.message,
    evidence: res.evidence,
    route: res.route,
    latency_ms: res.usage?.latency_ms ?? null,
    kb_ids: job.kbIds,
    insufficient: res.insufficient,
    usage: res.usage,
  }
  const existingAnswer = messages.findIndex((message) => message.id === assistant.id)
  if (existingAnswer >= 0) {
    const next = [...messages]
    next[existingAnswer] = assistant
    return next
  }
  const userMessage: ChatMessage = {
    id: -job.key,
    role: 'user',
    content: job.text,
    kb_ids: job.kbIds,
    created_at: assistant.created_at ?? new Date().toISOString(),
  }
  return [...messages, userMessage, assistant]
}

function upsertThinkingStep(steps: PipelineStep[], step: PipelineStep): PipelineStep[] {
  const index = steps.findIndex((item) => item.id === step.id)
  if (index < 0) return [...steps, step]
  const next = [...steps]
  next[index] = { ...next[index], ...step }
  return next
}

/** 把一条 SSE 事件合并进当前提问的流式状态。 */
function applyStreamEvent(job: AskJob, event: AskStreamEvent) {
  const stream = job.stream ?? (job.stream = { steps: [], answer: '', phase: 'thinking' })
  if (event.type === 'step') {
    stream.steps = upsertThinkingStep(stream.steps, {
      id: event.id,
      title: event.title,
      status: event.status,
      detail: event.detail,
    })
    return
  }
  if (event.type === 'answer') {
    stream.answer += event.delta
    stream.phase = 'answering'
    return
  }
  if (event.type === 'answer_replace') {
    stream.answer = event.content
    stream.phase = 'answering'
    return
  }
  if (event.type === 'done') {
    stream.phase = 'done'
  }
}

/** 置顶图钉：实心表示已置顶。 */
function PinIcon({ filled = false, size = 13 }: { filled?: boolean; size?: number }) {
  return (
    <svg
      viewBox="0 0 20 20"
      width={size}
      height={size}
      fill={filled ? 'currentColor' : 'none'}
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M7.4 2.8h5.2l-.6 4.3 2.5 2.1v1.2H5.5V9.2l2.5-2.1-.6-4.3Z" />
      <path d="M10 10.4v6.8" />
    </svg>
  )
}

/**
 * 会话条目的时间标签：今天只显示时分，昨天显示「昨天」，同年显示月/日。
 * 比完整时间戳短，才能在窄侧栏里和消息数并排，又不挤掉标题。
 */
function convTimeLabel(value: string | null): string {
  const d = parseApiTime(value)
  if (!d) return ''
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  if (d.toDateString() === now.toDateString()) return `${pad(d.getHours())}:${pad(d.getMinutes())}`
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (d.toDateString() === yesterday.toDateString()) return '昨天'
  if (d.getFullYear() === now.getFullYear()) return `${d.getMonth() + 1}/${d.getDate()}`
  return `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()}`
}

export default function ChatPage() {
  const { conversationId } = useParams<{ conversationId: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const { push } = useToast()
  const { user } = useAuth()
  const userId = user?.id ?? null
  const session = getToken()
  const parsedId = conversationId ? Number(conversationId) : null
  const activeId = parsedId && Number.isFinite(parsedId) ? parsedId : null

  const viewRef = useRef<{
    locationKey: string
    pathname: string
    visit: number
    intent: number
    id: number | null
    userId: number | null
    session: string | null
  } | null>(null)
  if (!viewRef.current || viewRef.current.locationKey !== location.key ||
      viewRef.current.pathname !== location.pathname || viewRef.current.userId !== userId ||
      viewRef.current.session !== session) {
    viewRef.current = {
      locationKey: location.key, pathname: location.pathname, visit: ++nextVisitKey,
      intent: 0, id: activeId, userId, session,
    }
  }
  const visit = viewRef.current.visit
  const mountedRef = useRef(false)
  const detailSequence = useRef(0)
  const detailInFlight = useRef<{ sequence: number; visit: number } | null>(null)
  const hydratedVisit = useRef<number | null>(null)
  const listSequence = useRef(0)
  const kbSequence = useRef(0)
  useLayoutEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      detailSequence.current += 1
      listSequence.current += 1
      kbSequence.current += 1
    }
  }, [])

  const isCurrentView = useCallback((expectedVisit: number, expectedId?: number | null) => {
    const view = viewRef.current
    const hashPath = window.location.hash.startsWith('#/')
      ? window.location.hash.slice(1).split('?')[0]
      : window.location.pathname
    return Boolean(mountedRef.current && view && view.visit === expectedVisit &&
      (expectedId === undefined || view.id === expectedId) &&
      view.pathname === hashPath && view.session === getToken())
  }, [])

  const [kbs, setKbs] = useState<KnowledgeBase[]>([])
  const [kbsOwner, setKbsOwner] = useState<{ userId: number | null; session: string | null } | null>(null)
  const [kbsLoading, setKbsLoading] = useState(true)
  const [kbsError, setKbsError] = useState<unknown>(null)

  // 问答模型下拉：默认选中模型设置里解析出的默认模型，用户可临时改选。
  const [modelOptions, setModelOptions] = useState<ChatModelOption[]>([])
  const [modelDefaultId, setModelDefaultId] = useState<number | null>(null)
  const [selectedModelId, setSelectedModelId] = useState<number | null>(null)
  const [modelsLoading, setModelsLoading] = useState(true)
  const [modelsError, setModelsError] = useState<unknown>(null)
  const modelSequence = useRef(0)

  const [conversations, setConversations] = useState<Conversation[]>([])
  const [convLoaded, setConvLoaded] = useState(false)
  const [convLoading, setConvLoading] = useState(true)
  const [convError, setConvError] = useState<unknown>(null)

  const [detail, setDetail] = useState<ConversationDetail | null>(null)
  const [detailVisit, setDetailVisit] = useState<number | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [reconcilingVisit, setReconcilingVisit] = useState<number | null>(null)
  const [detailError, setDetailError] = useState<unknown>(null)
  const [detailErrorVisit, setDetailErrorVisit] = useState<number | null>(null)

  const [selectedKbs, setSelectedKbs] = useState<number[]>([])
  const [question, setQuestion] = useState('')
  const [validationError, setValidationError] = useState<{ visit: number; text: string } | null>(null)
  const [, setJobVersion] = useState(0)
  const restoredErrors = useRef(new Set<number>())
  const restoredDrafts = useRef(new Set<number>())
  const recoveredJobKey = useRef<number | null>(null)

  const [renameTarget, setRenameTarget] = useState<Conversation | null>(null)
  const [renameText, setRenameText] = useState('')
  const [renameBusy, setRenameBusy] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<Conversation | null>(null)
  const [deleteBusy, setDeleteBusy] = useState(false)
  const [pinBusyId, setPinBusyId] = useState<number | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(false)

  const bottomRef = useRef<HTMLDivElement>(null)
  const newConversationRef = useRef<HTMLButtonElement>(null)
  const sidebarToggleRef = useRef<HTMLButtonElement>(null)
  const defaultsApplied = useRef(false)
  const stateOwner = useRef({ userId, session })

  useLayoutEffect(() => {
    // Old successful replies are already persisted; active requests settle or time out.
    for (const job of askJobs.values()) {
      if (job.session !== session && job.phase === 'success') forgetJob(job)
    }
  }, [userId, session])

  useLayoutEffect(() => {
    if (stateOwner.current.userId === userId && stateOwner.current.session === session) return
    stateOwner.current = { userId, session }
    defaultsApplied.current = false
    setKbs([])
    setKbsOwner(null)
    setKbsLoading(true)
    setKbsError(null)
    setModelOptions([])
    setModelDefaultId(null)
    setSelectedModelId(null)
    setModelsLoading(true)
    setModelsError(null)
    setConversations([])
    setConvLoaded(false)
    setConvLoading(true)
    setConvError(null)
    setDetail(null)
    setDetailVisit(null)
    setDetailError(null)
    setDetailErrorVisit(null)
    setReconcilingVisit(null)
    setSelectedKbs([])
    setQuestion('')
    setValidationError(null)
    setRenameTarget(null)
    setDeleteTarget(null)
  }, [userId, session])

  const loadKbs = useCallback(async () => {
    const sequence = ++kbSequence.current
    const requestSession = getToken()
    const requestUser = viewRef.current?.userId
    const current = () => mountedRef.current && sequence === kbSequence.current &&
      requestSession === getToken() && requestUser === viewRef.current?.userId
    setKbsLoading(true)
    try {
      const data = await kbApi.list()
      if (!current()) return
      setKbs(data.items ?? [])
      setKbsOwner({ userId: requestUser ?? null, session: requestSession })
      setKbsError(null)
    } catch (err) {
      if (current()) setKbsError(err)
    } finally {
      if (current()) setKbsLoading(false)
    }
  }, [])

  const loadConversations = useCallback(async () => {
    const sequence = ++listSequence.current
    const requestSession = getToken()
    const requestUser = viewRef.current?.userId
    const current = () => mountedRef.current && sequence === listSequence.current &&
      requestSession === getToken() && requestUser === viewRef.current?.userId
    setConvLoading(true)
    try {
      const data = await chatApi.conversations()
      if (!current()) return
      setConversations(data.items ?? [])
      setConvLoaded(true)
      setConvError(null)
    } catch (err) {
      if (current()) setConvError(err)
    } finally {
      if (current()) setConvLoading(false)
    }
  }, [])

  const loadModels = useCallback(async () => {
    const sequence = ++modelSequence.current
    const requestSession = getToken()
    const requestUser = viewRef.current?.userId
    const current = () => mountedRef.current && sequence === modelSequence.current &&
      requestSession === getToken() && requestUser === viewRef.current?.userId
    setModelsLoading(true)
    try {
      const data = await chatApi.models()
      if (!current()) return
      const items = data.items ?? []
      setModelOptions(items)
      setModelDefaultId(data.default_id ?? null)
      // 已选模型仍在列表里就保留；否则回到设置好的默认模型。
      setSelectedModelId((previous) =>
        previous !== null && items.some((m) => m.id === previous)
          ? previous
          : data.default_id ?? items[0]?.id ?? null)
      setModelsError(null)
    } catch (err) {
      if (!current()) return
      // 拉取失败（含旧版后端没有该接口）时降级：不指定模型，问答仍走全局默认。
      setModelsError(err)
      setModelOptions([])
      setModelDefaultId(null)
    } finally {
      if (current()) setModelsLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadKbs()
    void loadConversations()
    void loadModels()
  }, [loadKbs, loadConversations, loadModels, userId, session])

  useEffect(() => {
    if (sidebarOpen) newConversationRef.current?.focus()
  }, [sidebarOpen])

  const loadDetail = useCallback(async (id: number, silent = false, force = false, blocking = false) => {
    const view = viewRef.current
    if (!view || view.id !== id || !isCurrentView(view.visit, id)) return
    const requestVisit = view.visit
    if (silent && !force && detailInFlight.current?.visit === requestVisit) return
    const sequence = ++detailSequence.current
    detailInFlight.current = { sequence, visit: requestVisit }
    const current = () => sequence === detailSequence.current && isCurrentView(requestVisit, id)
    if (!silent) setDetailLoading(true)
    if (blocking) setReconcilingVisit(requestVisit)
    if (!silent) {
      setDetailError(null)
      setDetailErrorVisit(null)
    }
    try {
      const data = await chatApi.get(id)
      if (!current()) return
      const needsSelection = !silent || hydratedVisit.current !== requestVisit
      hydratedVisit.current = requestVisit
      setDetail(data)
      setDetailVisit(requestVisit)
      if (needsSelection) setSelectedKbs(data.kb_ids ?? [])
      setDetailError(null)
      setDetailErrorVisit(null)
      // Completed answers have exact IDs; a matching failed turn is only a hint.
      for (const job of askJobs.values()) {
        if (!jobMatchesSession(job, view.userId) || job.targetId !== id) continue
        if (job.phase === 'success' && data.messages.some((message) => message.id === job.response?.message.id)) {
          forgetJob(job)
        } else if (job.phase === 'error' && job.ambiguous && !job.matchingTurnSeen &&
                   serverHasQuestion(data.messages, job)) {
          // Matching text and KBs are only a hint; another tab could have sent them.
          job.matchingTurnSeen = true
          if (restoredDrafts.current.has(job.key)) {
            setQuestion((previous) => isCurrentView(requestVisit, id) && previous === job.text ? '' : previous)
            restoredDrafts.current.delete(job.key)
          }
          publishJob('change', job)
        }
      }
    } catch (err) {
      if (current() && (!silent || hydratedVisit.current !== requestVisit)) {
        setDetailError(err)
        setDetailErrorVisit(requestVisit)
        setDetail(null)
        setDetailVisit(null)
      }
    } finally {
      if (detailInFlight.current?.sequence === sequence) detailInFlight.current = null
      if (current()) {
        setDetailLoading(false)
        setReconcilingVisit(null)
      }
    }
  }, [isCurrentView])

  useEffect(() => {
    if (activeId) {
      void loadDetail(activeId)
    } else {
      detailSequence.current += 1
      setDetail(null)
      setDetailVisit(null)
      setDetailLoading(false)
      setDetailError(null)
      setDetailErrorVisit(null)
    }
  }, [activeId, visit, loadDetail])

  useEffect(() => {
    if (!activeId && !defaultsApplied.current && kbs.length > 0 &&
        kbsOwner?.userId === userId && kbsOwner.session === session) {
      defaultsApplied.current = true
      setSelectedKbs(kbs.map((k) => k.id))
    }
  }, [activeId, kbs, kbsOwner, userId, session])

  useEffect(() => {
    const listener = ({ kind, job, userId: eventUser, session: eventSession }: ChatJobEvent) => {
      if (eventUser !== userId || (eventSession !== getToken() && job?.phase !== 'error')) return
      if (job && !askJobs.has(job.key)) {
        restoredErrors.current.delete(job.key)
        restoredDrafts.current.delete(job.key)
        if (recoveredJobKey.current === job.key) recoveredJobKey.current = null
      }
      setJobVersion((version) => version + 1)
      if (kind === 'list' || kind === 'settled') void loadConversations()
      const view = viewRef.current
      if (kind === 'settled' && job?.targetId && view?.id === job.targetId &&
          isCurrentView(view.visit, job.targetId)) {
        void loadDetail(job.targetId, true, true, job.phase === 'error')
      }
    }
    jobListeners.add(listener)
    return () => { jobListeners.delete(listener) }
  }, [userId, isCurrentView, loadConversations, loadDetail])

  const currentJobs = [...askJobs.values()].filter((job) => jobMatchesSession(job, userId) &&
    (activeId ? job.targetId === activeId :
      job.phase === 'error' || (job.targetId === null && job.visit === visit && job.intent === viewRef.current?.intent)))
  const activeJob = currentJobs.find((job) => job.phase === 'creating' || job.phase === 'asking')
  const errorJobs = currentJobs.filter((job) => job.phase === 'error')
  const errorJob = activeJob ? undefined : [...errorJobs].reverse().find((job) =>
    activeId ? job.targetId === activeId : job.targetId === null)
  const successfulJobs = currentJobs.filter((job) => job.phase === 'success')
  const loadedDetail = detailVisit === visit && detail?.id === activeId ? detail : null
  const currentDetailError = detailErrorVisit === visit ? detailError : null
  const fallbackDetail = currentDetailError ? successfulJobs.find((job) => job.initialDetail)?.initialDetail : null
  const visibleDetail = loadedDetail ?? fallbackDetail ?? null
  const messages = successfulJobs.reduce((items, job) => withJobAnswer(items, job), visibleDetail?.messages ?? [])
  const pendingMessage = messages.find((message) => message.role === 'assistant' && message.status === 'pending') ?? null
  const asking = Boolean(activeJob)
  const streamingJob = activeJob && activeJob.stream ? activeJob : null
  // 流式期间服务端已落库的 pending 占位不渲染，避免「正在生成」气泡和
  // 思考面板同时出现；最终答案由 job.response 接管。
  const displayedMessages = streamingJob
    ? messages.filter((message) => !(message.role === 'assistant' && message.status === 'pending'))
    : messages
  const streamStepCount = streamingJob?.stream?.steps.length ?? 0
  const streamAnswerBucket = Math.floor((streamingJob?.stream?.answer.length ?? 0) / 160)
  const pending = activeJob && !serverHasQuestion(messages, activeJob) ? activeJob.text : null
  const detailReady = !activeId || Boolean(loadedDetail && !detailLoading && !currentDetailError)
  const reconciling = reconcilingVisit === visit
  const retryCandidate = errorJobs.find((job) => job.text === question.trim() &&
    (activeId ? job.targetId === activeId : job.targetId === null))
  const userJobCount = [...askJobs.values()].filter((job) => jobMatchesSession(job, userId)).length
  const capacityBlocked = userJobCount >= MAX_CHAT_JOBS && !retryCandidate
  const busy = !detailReady || reconciling || asking || Boolean(pendingMessage) || capacityBlocked
  const shownSelectedKbs = detailReady ? selectedKbs : []
  // 选中的模型若被停用/删除，回退到设置里的默认模型；没有可选模型时为 null。
  const effectiveModelId = modelOptions.some((m) => m.id === selectedModelId)
    ? selectedModelId
    : modelDefaultId ?? modelOptions[0]?.id ?? null
  const askError = validationError?.visit === visit ? validationError.text : ''
  const successfulJobKeys = successfulJobs.map((job) => job.key).join(',')

  useEffect(() => {
    for (const job of successfulJobs) visibleSuccessJobs.add(job.key)
    return () => { for (const job of successfulJobs) visibleSuccessJobs.delete(job.key) }
  }, [visit, successfulJobKeys])

  useEffect(() => {
    if (!errorJob || errorJob.matchingTurnSeen || restoredErrors.current.has(errorJob.key) ||
        !isCurrentView(visit, activeId) || reconciling ||
        (activeId && !detailReady && !currentDetailError)) return
    restoredErrors.current.add(errorJob.key)
    setQuestion((previous) => {
      if (previous || !isCurrentView(visit, activeId)) return previous
      restoredDrafts.current.add(errorJob.key)
      recoveredJobKey.current = errorJob.key
      return errorJob.text
    })
  }, [errorJob, activeId, detailReady, currentDetailError, reconciling, isCurrentView, visit])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages.length, asking, pending])

  // 流式回答逐段变长：每约 160 字跟一次底部，避免每个 token 都触发平滑滚动。
  useEffect(() => {
    if (!streamingJob) return
    bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [streamStepCount, streamAnswerBucket])

  const toggleKb = (id: number) => {
    if (!detailReady) return
    const next = selectedKbs.includes(id)
      ? selectedKbs.filter((x) => x !== id)
      : [...selectedKbs, id]
    setSelectedKbs(next)
    void persistKbSelection(next)
  }

  /** 把当前知识库选择写回会话（PATCH /conversations/{id}），失败只提示不回滚，避免操作抖动。 */
  const persistKbSelection = async (ids: number[]) => {
    if (!activeId || !detailReady) return
    try {
      await chatApi.update(activeId, { kb_ids: ids })
      if (!mountedRef.current || session !== getToken()) return
      setConversations((prev) =>
        prev.map((c) => (c.id === activeId ? { ...c, kb_ids: ids } : c)),
      )
      if (isCurrentView(visit, activeId)) {
        setDetail((prev) => (prev && prev.id === activeId ? { ...prev, kb_ids: ids } : prev))
      }
    } catch (err) {
      if (session === getToken()) push(`知识库选择未能保存：${errorText(err)}`, 'error')
    }
  }

  const clearKbSelection = () => {
    if (!detailReady) return
    setSelectedKbs([])
    void persistKbSelection([])
  }

  const closeSidebar = () => {
    setSidebarOpen(false)
    sidebarToggleRef.current?.focus()
  }

  const newConversation = async () => {
    if (!viewRef.current) return
    const intent = ++viewRef.current.intent
    setJobVersion((version) => version + 1)
    const kbIds = detailReady ? selectedKbs : []
    try {
      const title = `新会话 ${new Date().toLocaleString('zh-CN', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
      })}`
      const created = await chatApi.create(title, kbIds)
      if (session !== getToken()) return
      publishList(userId, session)
      if (isCurrentView(visit, activeId) && viewRef.current?.intent === intent) {
        if (sidebarOpen) closeSidebar()
        navigate(`/chat/${created.id}`)
      }
    } catch (err) {
      if (session === getToken()) push(errorText(err), 'error')
    }
  }

  const ask = async () => {
    if (busy || !isCurrentView(visit, activeId)) return
    // The registry is updated synchronously, so two clicks in one render cannot submit twice.
    if ([...askJobs.values()].some((job) => jobMatchesSession(job, userId) &&
      (activeId ? job.targetId === activeId :
        job.targetId === null && job.visit === visit && job.intent === viewRef.current?.intent) &&
      (job.phase === 'creating' || job.phase === 'asking'))) return
    const text = question.trim()
    if (!text) {
      setValidationError({ visit, text: '请输入问题' })
      return
    }
    if (selectedKbs.length === 0) {
      setValidationError({ visit, text: '请至少选择一个知识库' })
      return
    }
    const recovered = errorJobs.find((job) => job.key === recoveredJobKey.current &&
      (activeId ? job.targetId === activeId : job.targetId === null)) ?? retryCandidate
    if (recovered) forgetJob(recovered)
    if ([...askJobs.values()].filter((job) => jobMatchesSession(job, userId)).length >= MAX_CHAT_JOBS) {
      setValidationError({ visit, text: '待处理提问过多，请先处理已有问题或等待其他提问完成。' })
      return
    }
    recoveredJobKey.current = null
    setValidationError(null)
    setQuestion('')
    const originIntent = ++viewRef.current!.intent
    const job: AskJob = {
      key: ++nextJobKey, userId, session, visit, intent: originIntent, targetId: activeId, text,
      kbIds: [...selectedKbs], baselineId: Math.max(0, ...messages.map((message) => message.id)),
      initialDetail: loadedDetail ? { ...loadedDetail, messages } : null,
      phase: activeId ? 'asking' : 'creating',
    }
    askJobs.set(job.key, job)
    publishJob('change', job)

    try {
      if (!job.targetId) {
        const title = text.slice(0, 28) + (text.length > 28 ? '…' : '')
        const created = await chatApi.create(title, job.kbIds)
        if (askJobs.get(job.key) !== job) return
        job.targetId = created.id
        job.initialDetail = { ...created, messages: [] }
        if (session !== getToken()) {
          job.error = '登录状态已更改，请重新登录后恢复提问。'
          job.ambiguous = false
          job.phase = 'error'
          publishJob('change', job)
          return
        }
        job.phase = 'asking'
        publishJob('list', job)
        if (isCurrentView(visit, null) && viewRef.current?.intent === originIntent) {
          navigate(`/chat/${created.id}`, { replace: true })
        }
      }
      // A create may have finished after logout; never submit with a different session's token.
      if (session !== getToken()) {
        job.error = '登录状态已更改，请重新登录后恢复提问。'
        job.ambiguous = false
        job.phase = 'error'
        publishJob('change', job)
        return
      }
      // 先把思考面板挂出来，再发请求：即使后端还在分析意图，用户也能看到
      // 「正在理解问题」，而不是一个没有反馈的转圈。
      job.stream = { steps: [], answer: '', phase: 'thinking' }
      publishJob('change', job)
      const res = await chatApi.askStream(job.targetId, text, job.kbIds, (event) => {
        if (askJobs.get(job.key) !== job || session !== getToken()) return
        applyStreamEvent(job, event)
        publishJob('change', job)
      }, effectiveModelId)
      if (askJobs.get(job.key) !== job || session !== getToken()) {
        forgetJob(job)
        return
      }
      job.response = res
      job.stream = undefined
      job.phase = 'success'
      if (res.denied && res.denied.length > 0) {
        push(
          `以下知识库本次被跳过：${res.denied.map((d) => `${d.kb_name}（${d.reason}）`).join('；')}`,
          'error',
        )
      }
      publishJob('settled', job)
      retainSettledJob(job)
    } catch (err) {
      if (askJobs.get(job.key) !== job) return
      // request() clears the token before throwing 401; that rejection is pre-write.
      if (err instanceof ApiError && err.status === 401) {
        job.error = errorText(err)
        job.ambiguous = false
        job.phase = 'error'
        publishJob('change', job)
        return
      }
      const sameSession = session === getToken()
      job.error = errorText(err)
      job.ambiguous = job.phase === 'asking' && !(err instanceof ApiError && [403, 404, 422].includes(err.status))
      job.phase = 'error'
      publishJob(sameSession ? 'settled' : 'change', job)
    }
  }

  const doRename = async () => {
    if (!renameTarget) return
    const targetId = renameTarget.id
    const title = renameText.trim()
    if (!title) return
    setRenameBusy(true)
    try {
      await chatApi.update(targetId, { title })
      if (session !== getToken()) return
      publishList(userId, session)
      const view = viewRef.current
      if (view?.id === targetId && isCurrentView(view.visit, targetId)) {
        setDetail((previous) => previous?.id === targetId ? { ...previous, title } : previous)
        void loadDetail(targetId, true, true)
      }
      if (mountedRef.current) setRenameTarget(null)
      push('会话已重命名', 'success')
    } catch (err) {
      if (session === getToken()) push(errorText(err), 'error')
    } finally {
      if (mountedRef.current && session === getToken()) setRenameBusy(false)
    }
  }

  const doDelete = async () => {
    if (!deleteTarget) return
    const targetId = deleteTarget.id
    setDeleteBusy(true)
    try {
      await chatApi.remove(targetId)
      if (session !== getToken()) return
      for (const job of askJobs.values()) {
        if (jobMatchesSession(job, userId) && job.targetId === targetId) forgetJob(job)
      }
      publishList(userId, session)
      push('会话已删除', 'success')
      if (mountedRef.current) setDeleteTarget(null)
      const view = viewRef.current
      if (view?.id === targetId && isCurrentView(view.visit, targetId)) {
        detailSequence.current += 1
        navigate('/chat', { replace: true })
      }
    } catch (err) {
      if (session === getToken()) push(errorText(err), 'error')
    } finally {
      if (mountedRef.current && session === getToken()) setDeleteBusy(false)
    }
  }

  const togglePin = async (conv: Conversation) => {
    if (pinBusyId !== null) return
    const pinned = !conv.pinned
    setPinBusyId(conv.id)
    try {
      await chatApi.update(conv.id, { pinned })
      if (session !== getToken()) return
      // 排序由服务端负责（置顶优先、最近更新优先），切换后重新取列表即可。
      await loadConversations()
      if (session === getToken()) push(pinned ? '会话已置顶' : '已取消置顶', 'success')
    } catch (err) {
      if (session === getToken()) push(errorText(err), 'error')
    } finally {
      if (mountedRef.current && session === getToken()) setPinBusyId(null)
    }
  }

  const openWikiPage = (kbId: number, slug: string) => {
    navigate(`/kbs/${kbId}?tab=wiki&slug=${encodeURIComponent(slug)}`)
  }

  const visibleKbs = kbs
  const pinnedCount = conversations.filter((c) => c.pinned).length

  // A pending server message also covers a page reload or an HTTP timeout.
  useEffect(() => {
    if (!activeId || !pendingMessage || asking) return
    const timer = window.setInterval(() => {
      void loadDetail(activeId, true)
    }, 2000)
    return () => window.clearInterval(timer)
  }, [activeId, visit, pendingMessage?.id, asking, loadDetail])

  return (
    <>
      <div className="chat-shell">
        <aside
          id="chat-conversation-sidebar"
          className={`chat-sidebar ${sidebarOpen ? 'open' : ''}`}
          onKeyDown={(e) => {
            if (e.key === 'Escape' && sidebarOpen) closeSidebar()
          }}
        >
          <div className="chat-sidebar-head">
            <button
              type="button"
              ref={newConversationRef}
              className="btn btn-primary btn-sm full"
              onClick={() => void newConversation()}
            >
              + 新建会话
            </button>
            <button
              type="button"
              className="icon-btn mobile-only chat-sidebar-close"
              onClick={closeSidebar}
              aria-label="关闭会话列表"
            >
              <IconClose size={16} />
            </button>
          </div>
          <div className="conv-list chat-conv-list">
            {convError ? (
              <div className="tiny" style={{ padding: 10, color: 'var(--c-danger)' }}>
                会话列表加载失败 <button type="button" className="btn-link" onClick={() => void loadConversations()}>重试会话列表</button>
              </div>
            ) : null}
            {convLoading && !convLoaded ? (
              <div style={{ padding: 8 }}>
                <SkeletonLines rows={4} />
              </div>
            ) : !convLoaded && convError ? null : conversations.length === 0 ? (
              <div className="chat-conv-empty">还没有会话</div>
            ) : (
              conversations.map((c, index) => (
                <Fragment key={c.id}>
                  {c.pinned && index === 0 ? <div className="chat-conv-group">置顶</div> : null}
                  {!c.pinned && pinnedCount > 0 && index === pinnedCount ? (
                    <div className="chat-conv-group">最近会话</div>
                  ) : null}
                  <div className={`chat-conv-item ${activeId === c.id ? 'active' : ''}`}>
                    <Link
                      to={`/chat/${c.id}`}
                      className="chat-conv-link"
                      title={c.title}
                      aria-current={activeId === c.id ? 'page' : undefined}
                      onClick={() => {
                        if (viewRef.current && activeId !== c.id) viewRef.current.intent += 1
                        if (sidebarOpen) closeSidebar()
                      }}
                    >
                      <span className="chat-conv-title">
                        {c.pinned ? (
                          <span className="chat-conv-pin" aria-hidden="true"><PinIcon filled size={12} /></span>
                        ) : null}
                        <span className="chat-conv-title-text">{c.title}</span>
                      </span>
                      <span className="chat-conv-meta">
                        {convTimeLabel(c.updated_at)} · {c.message_count > 0 ? `${c.message_count} 条消息` : '暂无消息'}
                      </span>
                    </Link>
                    <span className="chat-conv-actions">
                      <button
                        type="button"
                        title={c.pinned ? '取消置顶' : '置顶会话'}
                        aria-label={`${c.pinned ? '取消置顶' : '置顶'}会话：${c.title}`}
                        aria-pressed={c.pinned}
                        disabled={pinBusyId === c.id}
                        onClick={() => void togglePin(c)}
                      >
                        <PinIcon filled={c.pinned} />
                      </button>
                      <button
                        type="button"
                        title="重命名"
                        aria-label={`重命名会话：${c.title}`}
                        onClick={() => {
                          setRenameTarget(c)
                          setRenameText(c.title)
                        }}
                      >
                        <svg
                          viewBox="0 0 20 20"
                          width="14"
                          height="14"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="1.5"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          aria-hidden="true"
                        >
                          <path d="m3.5 16.5 3.3-.7 8.7-8.7a2.1 2.1 0 0 0-3-3l-8.3 8.7-.7 3.7ZM11.7 5.3l3 3" />
                        </svg>
                      </button>
                      <button
                        type="button"
                        title="删除"
                        aria-label={`删除会话：${c.title}`}
                        onClick={() => setDeleteTarget(c)}
                      >
                        <svg
                          viewBox="0 0 20 20"
                          width="14"
                          height="14"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="1.5"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          aria-hidden="true"
                        >
                          <path d="M4 5.5h12M7.5 5.5V4h5v1.5M5.5 5.5l.6 10.2c0 .7.6 1.3 1.3 1.3h5.2c.7 0 1.3-.6 1.3-1.3l.6-10.2M8 8.5v5.5M12 8.5v5.5" />
                        </svg>
                      </button>
                    </span>
                  </div>
                </Fragment>
              ))
            )}
          </div>
        </aside>

        <section className="chat-main">
          <div className="chat-toolbar">
            <button
              type="button"
              className="icon-btn mobile-only"
              ref={sidebarToggleRef}
              onClick={() => (sidebarOpen ? closeSidebar() : setSidebarOpen(true))}
              aria-label="会话列表"
              aria-controls="chat-conversation-sidebar"
              aria-expanded={sidebarOpen}
            >
              <IconMenu size={18} />
            </button>
            <span className="tiny muted nowrap">知识库：</span>
            <div className="kb-selector">
              {kbsLoading ? (
                <span className="tiny muted">加载中…</span>
              ) : kbsError ? (
                <span className="tiny" style={{ color: 'var(--c-danger)' }}>
                  知识库加载失败
                </span>
              ) : visibleKbs.length === 0 ? (
                <span className="tiny muted">暂无可选知识库，请联系管理员授权</span>
              ) : (
                visibleKbs.map((k) => (
                  <button
                    key={k.id}
                    type="button"
                    className={`kb-pick ${shownSelectedKbs.includes(k.id) ? 'on' : ''}`}
                    aria-pressed={shownSelectedKbs.includes(k.id)}
                    disabled={!detailReady}
                    onClick={() => toggleKb(k.id)}
                    title={`${k.name} · ${k.mode_label}`}
                  >
                    {k.name}
                    <span className="lv">{k.my_level === 'manage' ? '管理' : '只读'}</span>
                  </button>
                ))
              )}
            </div>
            <div className="spacer" />
            <label className="chat-model-picker">
              <span className="tiny muted nowrap">模型：</span>
              <Select
                className="chat-model-select"
                value={effectiveModelId ?? ''}
                onChange={(e) => setSelectedModelId(e.target.value ? Number(e.target.value) : null)}
                disabled={modelsLoading || modelOptions.length === 0}
                aria-label="选择回答使用的模型"
                title={modelsError ? '模型列表加载失败，将使用设置里的默认模型' : '选择本次回答使用的模型'}
              >
                {modelOptions.length === 0 ? (
                  <option value="">{modelsLoading ? '加载中…' : '默认模型'}</option>
                ) : modelOptions.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.display}{m.id === modelDefaultId ? '（默认）' : ''}
                  </option>
                ))}
              </Select>
            </label>
            {shownSelectedKbs.length > 0 ? (
              <button type="button" className="btn-link" onClick={clearKbSelection}>
                清空选择
              </button>
            ) : null}
          </div>

          <div className="chat-scroll">
            <div className="chat-thread">
              {currentDetailError && visibleDetail && !detailLoading ? (
                <ErrorState error={currentDetailError} onRetry={() => activeId && void loadDetail(activeId)} />
              ) : null}
              {detailLoading || (activeId && detailVisit !== visit && !currentDetailError) ? (
                <div className="card card-pad">
                  <SkeletonLines rows={5} />
                </div>
              ) : currentDetailError && !visibleDetail ? (
                <ErrorState error={currentDetailError} onRetry={() => activeId && void loadDetail(activeId)} />
              ) : displayedMessages.length === 0 && !pending && !streamingJob ? (
                <div className="chat-empty">
                  <div className="chat-empty-icon"><IconMessage size={22} /></div>
                  <div className="chat-empty-title">向知识库提问</div>
                  <div className="chat-empty-hint">
                    {shownSelectedKbs.length === 0
                      ? '先在上方选择要检索的知识库，然后输入问题。'
                      : `已选择 ${shownSelectedKbs.length} 个知识库。回答会附带证据原文、文档版本与页码，可点开核对。`}
                  </div>
                  {shownSelectedKbs.length > 0 ? (
                    <div className="chat-samples">
                      {SAMPLE_QUESTIONS.map((q) => (
                        <button
                          key={q}
                          type="button"
                          className="sample-chip"
                          onClick={() => setQuestion(q)}
                        >
                          {q}
                        </button>
                      ))}
                    </div>
                  ) : null}
                  <div className="chat-empty-tip">Enter 发送 · Shift+Enter 换行 · 回答基于检索到的证据</div>
                </div>
              ) : (
                <>
                  {displayedMessages.map((m) => (
                    <MessageBubble
                      key={m.id}
                      message={m}
                      onOpenWikiPage={openWikiPage}
                      userName={user?.display_name || user?.username || '我'}
                    />
                  ))}
                  {streamingJob ? (
                    <>
                      {pending ? (
                        <MessageBubble
                          message={{
                            id: -1,
                            role: 'user',
                            content: pending,
                            created_at: new Date().toISOString(),
                          }}
                          userName={user?.display_name || user?.username || '我'}
                        />
                      ) : null}
                      {streamingJob.stream ? <StreamingBubble stream={streamingJob.stream} /> : null}
                    </>
                  ) : pending ? (
                    <>
                      <MessageBubble
                        message={{
                          id: -1,
                          role: 'user',
                          content: pending,
                          created_at: new Date().toISOString(),
                        }}
                        userName={user?.display_name || user?.username || '我'}
                      />
                      <GeneratingBubble />
                    </>
                  ) : null}
                </>
              )}
              <div ref={bottomRef} />
            </div>
          </div>

          <div className="chat-composer">
            {askError || capacityBlocked ? (
              <div style={{ maxWidth: 900, margin: '0 auto 8px', color: 'var(--c-danger)', fontSize: 12.5 }}>
                {askError || '待处理提问过多，请先处理已有问题或等待其他提问完成。'}
              </div>
            ) : null}
            {errorJobs.length ? (
              <div aria-label="待处理的提问" style={{
                maxWidth: 900, margin: '0 auto 8px', background: 'var(--c-surface-alt)',
                border: '1px solid var(--c-border)', borderRadius: 8,
              }}>
                <div style={{
                  padding: '8px 12px', borderBottom: '1px solid var(--c-border)',
                  color: 'var(--c-text-secondary)', fontSize: 12, fontWeight: 600,
                }}>待处理的提问</div>
                <div style={{ maxHeight: 'min(230px, 25vh)', overflowY: 'auto', padding: '0 10px 10px' }}>
                  {errorJobs.map((job) => (
                    <div key={job.key} role="group" aria-label={`待处理提问：${job.text}`}
                      style={{
                        marginTop: 8, padding: '9px 10px', border: '1px solid var(--c-border)',
                        borderRadius: 6, background: 'var(--c-surface)', fontSize: 12.5,
                      }}>
                      <div style={{ color: 'var(--c-text-secondary)', lineHeight: 1.5, overflowWrap: 'anywhere' }}>
                        <span>{job.ambiguous ? '提问结果尚未确认，请先核对会话记录。' : '这次提问未发送。'}</span>{' '}
                        {!job.matchingTurnSeen ? <span style={{ color: 'var(--c-danger)' }}>{job.error}</span> : null}
                        {job.matchingTurnSeen ? <span> 会话中有相同提问，请核对后再重试。</span> : null}
                      </div>
                      <div style={{
                        color: 'var(--c-text)', lineHeight: 1.5, marginTop: 6,
                        whiteSpace: 'pre-wrap', overflowWrap: 'anywhere',
                      }}>{job.text}</div>
                      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 16px', marginTop: 7 }}>
                        {job.targetId && job.targetId !== activeId ? (
                          <button type="button" className="btn-link" aria-label={`查看会话中的提问：${job.text}`}
                            onClick={() => {
                              if (viewRef.current) viewRef.current.intent += 1
                              navigate(`/chat/${job.targetId}`)
                            }}>查看会话</button>
                        ) : (
                          <button type="button" className="btn-link" aria-label={`恢复未发送的问题：${job.text}`}
                            onClick={() => {
                              restoredDrafts.current.add(job.key)
                              recoveredJobKey.current = job.key
                              setQuestion(job.text)
                            }}>恢复未发送的问题</button>
                        )}
                        <button type="button" className="btn-link" aria-label={`忽略这条提问：${job.text}`}
                          onClick={() => forgetJob(job)}>忽略这条提问</button>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}
            <div className="composer-inner">
              <textarea
                className="composer-input"
                aria-label="提问输入框"
                value={question}
                onChange={(e) => {
                  if (recoveredJobKey.current !== null) restoredDrafts.current.delete(recoveredJobKey.current)
                  setQuestion(e.target.value)
                }}
                placeholder={
                  shownSelectedKbs.length === 0
                    ? '请先在上方选择知识库…'
                    : '输入你的问题，Enter 发送，Shift+Enter 换行'
                }
                disabled={busy}
                onKeyDown={(e) => {
                  // 229 是部分输入法在组合文字时使用的键码。
                  const composing = e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229
                  if (e.key === 'Enter' && !e.shiftKey && !composing) {
                    e.preventDefault()
                    if (!busy) void ask()
                  }
                }}
              />
              <Button
                variant="primary"
                loading={asking || Boolean(pendingMessage)}
                disabled={busy || !question.trim()}
                onClick={() => void ask()}
                style={{ height: 44, minWidth: 84 }}
              >
                发送
              </Button>
            </div>
            <div className="composer-hint">
              {reconciling ? <span>正在核对本次提问…</span> : null}
              <span>已选 {shownSelectedKbs.length} 个知识库</span>
              <span>回答基于检索到的证据，可能不完整，请核对来源</span>
              {visibleDetail ? <span>会话 #{visibleDetail.id}</span> : null}
            </div>
          </div>
        </section>
      </div>

      <Modal
        open={Boolean(renameTarget)}
        title="重命名会话"
        onClose={() => setRenameTarget(null)}
        width={420}
        footer={
          <>
            <Button variant="ghost" onClick={() => setRenameTarget(null)} disabled={renameBusy}>
              取消
            </Button>
            <Button variant="primary" loading={renameBusy} onClick={() => void doRename()}>
              保存
            </Button>
          </>
        }
      >
        <TextInput
          value={renameText}
          onChange={(e) => setRenameText(e.target.value)}
          aria-label="会话名称"
          autoFocus
          maxLength={80}
        />
      </Modal>

      <Modal
        open={Boolean(deleteTarget)}
        title="删除会话"
        onClose={() => setDeleteTarget(null)}
        width={420}
        footer={
          <>
            <Button variant="ghost" onClick={() => setDeleteTarget(null)} disabled={deleteBusy}>
              取消
            </Button>
            <Button variant="danger" loading={deleteBusy} onClick={() => void doDelete()}>
              删除
            </Button>
          </>
        }
      >
        <div className="confirm-text">
          确认删除会话「{deleteTarget?.title}」？该会话的消息记录会一并移除，此操作不可撤销。
        </div>
      </Modal>
    </>
  )
}

/** 空会话时的示例问题：只填进输入框，不直接发送，用户可以改。 */
const SAMPLE_QUESTIONS = [
  '出差住宿标准是多少？',
  '这个流程需要谁审批？',
  '产品的定价是怎么定的？',
]

/** 「正在检索并生成回答」占位：本页刚发出的提问与服务端 pending 消息共用同一套样式。 */
function GeneratingBubble() {
  return (
    <div className="msg msg-assistant">
      <div className="msg-avatar">AI</div>
      <div className="msg-main">
        <div className="msg-bubble">
          <div className="row" style={{ gap: 8 }}>
            <Spinner size={14} />
            <span className="muted">正在检索并生成回答…</span>
          </div>
          <div style={{ marginTop: 10 }}>
            <SkeletonLines rows={2} />
          </div>
        </div>
      </div>
    </div>
  )
}

/** 流式问答：实时展示检索链路的每一步，答案增量直接渲染在下面。 */
function StreamingBubble({ stream }: { stream: ThinkingStream }) {
  const [open, setOpen] = useState(true)
  const done = stream.phase === 'done'
  return (
    <div className="msg msg-assistant">
      <div className="msg-avatar">AI</div>
      <div className="msg-main">
        <div className="thinking-card">
          <button
            type="button"
            className="thinking-head"
            onClick={() => setOpen((value) => !value)}
            aria-expanded={open}
          >
            <span className={`thinking-pulse ${done ? 'done' : ''}`} />
            <span className="thinking-title">{done ? '思考完成' : '正在思考'}</span>
            <span className="tiny muted">{stream.steps.length} 个步骤</span>
            <span className="spacer" />
            {open ? <IconChevronDown size={13} /> : <IconChevronRight size={13} />}
          </button>
          {open ? (
            <div className="thinking-steps">
              {stream.steps.map((step) => (
                <div key={step.id} className={`thinking-step ${step.status}`}>
                  <span className="thinking-dot" />
                  <div style={{ minWidth: 0 }}>
                    <div className="thinking-step-title">{step.title}</div>
                    {step.detail ? <div className="thinking-step-detail">{step.detail}</div> : null}
                  </div>
                </div>
              ))}
              {stream.steps.length === 0 ? (
                <div className="tiny muted">正在连接检索链路…</div>
              ) : null}
            </div>
          ) : null}
        </div>
        {stream.answer ? (
          <div className="msg-bubble" style={{ marginTop: 10 }}>
            <Markdown content={stream.answer} />
          </div>
        ) : !done ? (
          <div className="tiny muted" style={{ marginTop: 8 }}>正在检索知识库并组织答案…</div>
        ) : null}
      </div>
    </div>
  )
}

function MessageBubble({
  message,
  userName,
  onOpenWikiPage,
}: {
  message: ChatMessage
  userName: string
  onOpenWikiPage?: (kbId: number, slug: string) => void
}) {
  const isUser = message.role === 'user'
  const evidence = message.evidence ?? []
  const route = message.route ?? null
  // 拒答判定只看后端字段：历史消息与当次回答同口径（serializers.message_out 会回填）。
  // 不再用「没有证据」近似猜测——正常回答也可能一次没召回证据。
  const insufficient = message.insufficient ?? false

  // 服务端还在生成的回答：离开页面再回来时由它接住「正在生成」，不必等用户刷新。
  if (!isUser && message.status === 'pending') {
    return <GeneratingBubble />
  }

  // 生成失败/被中断：把后端给的说明直接展示出来，并明确标注「未完成」，
  // 避免把一条空回答误当成正常结果。
  if (!isUser && message.status === 'failed') {
    return (
      <div className="msg msg-assistant">
        <div className="msg-avatar">AI</div>
        <div className="msg-main">
          <div className="msg-bubble msg-failed">
            {message.content || '这次回答没有生成完成，可以重新提问。'}
          </div>
          <div className="msg-meta">
            <span>{formatTime(message.created_at)}</span>
            <span>未完成</span>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className={`msg ${isUser ? 'msg-user' : 'msg-assistant'}`}>
      <div className="msg-avatar">{isUser ? userName.slice(0, 1) : 'AI'}</div>
      <div className="msg-main">
        <div className="msg-bubble">
          {isUser ? (
            <div style={{ whiteSpace: 'pre-wrap' }}>{message.content}</div>
          ) : (
            <Markdown content={message.content} />
          )}
        </div>
        {isUser ? (
          <div className="msg-meta">
            <span>{formatTime(message.created_at)}</span>
            {message.kb_ids?.length ? <span>检索 {message.kb_ids.length} 个库</span> : null}
          </div>
        ) : (
          <>
            <div className="msg-meta">
              <span>{formatTime(message.created_at)}</span>
              {message.latency_ms ? <span>{formatMs(message.latency_ms)}</span> : null}
            </div>
            <MessageInsights
              evidence={evidence}
              route={route}
              usage={message.usage}
              latencyMs={message.latency_ms}
              insufficient={insufficient}
              onOpenWikiPage={onOpenWikiPage}
            />
          </>
        )}
      </div>
    </div>
  )
}

import { useCallback, useEffect, useRef, useState } from 'react'
import type { Dispatch, MutableRefObject, SetStateAction } from 'react'
import { chatApi, getToken, kbApi } from '../../api'
import type {
  ChatModelOption,
  ChatModelOptions,
  Conversation,
  ConversationDetail,
  KnowledgeBase,
} from '../../types'
import { askJobs, forgetJob, jobMatchesSession, publishJob, serverHasQuestion } from './jobs'
import type { ChatView } from './useChatView'

/**
 * 本页的数据层：知识库列表、可选模型、会话列表、会话详情。
 * 状态放在自定义 hook 里，ChatPage 的组件实例与生命周期不变；
 * 所有请求仍靠访问序号 + isCurrentView 丢弃过期响应。
 */
export function useChatData({
  userId,
  session,
  activeId,
  visit,
  isCurrentView,
  viewRef,
  mountedRef,
  detailSequence,
  detailInFlight,
  hydratedVisit,
  listSequence,
  kbSequence,
  defaultsApplied,
  restoredDrafts,
  setQuestion,
}: {
  userId: number | null
  session: string | null
  activeId: number | null
  visit: number
  isCurrentView: (expectedVisit: number, expectedId?: number | null) => boolean
  viewRef: MutableRefObject<ChatView | null>
  mountedRef: MutableRefObject<boolean>
  detailSequence: MutableRefObject<number>
  detailInFlight: MutableRefObject<{ sequence: number; visit: number } | null>
  hydratedVisit: MutableRefObject<number | null>
  listSequence: MutableRefObject<number>
  kbSequence: MutableRefObject<number>
  defaultsApplied: MutableRefObject<boolean>
  restoredDrafts: MutableRefObject<Set<number>>
  setQuestion: Dispatch<SetStateAction<string>>
}) {
  const [kbs, setKbs] = useState<KnowledgeBase[]>([])
  const [kbsOwner, setKbsOwner] = useState<{ userId: number | null; session: string | null } | null>(null)
  const [kbsLoading, setKbsLoading] = useState(true)
  const [kbsError, setKbsError] = useState<unknown>(null)

  // null 跟随设置中的默认模型；显式选择的模型只用于当前页面。
  const [modelOptions, setModelOptions] = useState<ChatModelOption[]>([])
  const [modelDefaultId, setModelDefaultId] = useState<number | null>(null)
  const [selectedModelId, setSelectedModelId] = useState<number | null>(null)
  const [modelsLoading, setModelsLoading] = useState(true)
  const [modelsError, setModelsError] = useState<unknown>(null)
  const modelSequence = useRef(0)
  const modelRequest = useRef<{
    userId: number | null; session: string | null; sequence: number
    promise: Promise<ChatModelOptions | null>
  } | null>(null)

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

  const loadModels = useCallback((): Promise<ChatModelOptions | null> => {
    const requestSession = getToken()
    const requestUser = viewRef.current?.userId ?? null
    const pending = modelRequest.current
    // 聚焦与提交同时触发时共用请求，避免显示和提交不同的默认模型。
    if (pending && pending.session === requestSession && pending.userId === requestUser) return pending.promise
    const sequence = ++modelSequence.current
    const current = () => mountedRef.current && sequence === modelSequence.current &&
      requestSession === getToken() && requestUser === viewRef.current?.userId
    setModelsLoading(true)
    const promise = (async () => {
      try {
        const data = await chatApi.models()
        if (!current()) return null
        const items = data.items ?? []
        setModelOptions(items)
        setModelDefaultId(data.default_id ?? null)
        // 已选模型失效时回到默认，未显式选择时始终跟随服务端默认值。
        setSelectedModelId((previous) =>
          previous !== null && items.some((m) => m.id === previous) ? previous : null)
        setModelsError(null)
        return data
      } catch (err) {
        if (current()) setModelsError(err)
        return null
      } finally {
        if (modelRequest.current?.sequence === sequence) modelRequest.current = null
        if (current()) setModelsLoading(false)
      }
    })()
    modelRequest.current = { userId: requestUser, session: requestSession, sequence, promise }
    return promise
  }, [])

  useEffect(() => {
    void loadKbs()
    void loadConversations()
    void loadModels()
  }, [loadKbs, loadConversations, loadModels, userId, session])

  useEffect(() => {
    const refresh = () => { if (document.visibilityState === 'visible') void loadModels() }
    window.addEventListener('focus', refresh)
    document.addEventListener('visibilitychange', refresh)
    return () => {
      window.removeEventListener('focus', refresh)
      document.removeEventListener('visibilitychange', refresh)
    }
  }, [loadModels])

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

  const resetChatData = useCallback(() => {
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
  }, [])

  return {
    kbs,
    kbsLoading,
    kbsError,
    modelOptions,
    modelDefaultId,
    selectedModelId,
    setSelectedModelId,
    modelsLoading,
    modelsError,
    conversations,
    setConversations,
    convLoaded,
    convLoading,
    convError,
    detail,
    setDetail,
    detailVisit,
    detailLoading,
    reconcilingVisit,
    detailError,
    detailErrorVisit,
    selectedKbs,
    setSelectedKbs,
    modelRequest,
    loadConversations,
    loadModels,
    loadDetail,
    resetChatData,
  }
}

import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { useAuth, useToast } from '../auth'
import { ApiError, chatApi, getToken } from '../api'
import type { Conversation } from '../types'
import { errorText } from '../utils'
import { SESSION_SIGNED_OUT } from '../shared/messages'
import { Button, ErrorState, Modal, SkeletonLines, TextInput } from '../components/ui'
import { ChatComposer, ChatToolbar } from './chat/Composer'
import { ConversationList } from './chat/ConversationList'
import { EmptyThread, GeneratingBubble, MessageBubble, StreamingBubble } from './chat/MessageList'
import { useAskJobs } from './chat/useAskJobs'
import { useChatData } from './chat/useChatData'
import { useChatView } from './chat/useChatView'
import {
  MAX_CHAT_JOBS,
  applyStreamEvent,
  askJobs,
  forgetJob,
  jobMatchesSession,
  newJobKey,
  publishJob,
  publishList,
  retainSettledJob,
  serverHasQuestion,
  withJobAnswer,
} from './chat/jobs'
import type { AskJob } from './chat/jobs'

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

  const {
    viewRef, mountedRef, detailSequence, detailInFlight, hydratedVisit, listSequence, kbSequence,
    visit, isCurrentView,
  } = useChatView({
    locationKey: location.key,
    pathname: location.pathname,
    id: activeId,
    userId,
    session,
  })

  const [question, setQuestion] = useState('')
  const [validationError, setValidationError] = useState<{ visit: number; text: string } | null>(null)
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

  const {
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
  } = useChatData({
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
  })

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
    resetChatData()
    setQuestion('')
    setValidationError(null)
    setRenameTarget(null)
    setDeleteTarget(null)
  }, [userId, session])

  useEffect(() => {
    if (sidebarOpen) newConversationRef.current?.focus()
  }, [sidebarOpen])

  const loadedDetail = detailVisit === visit && detail?.id === activeId ? detail : null
  const currentDetailError = detailErrorVisit === visit ? detailError : null
  const detailReady = !activeId || Boolean(loadedDetail && !detailLoading && !currentDetailError)
  const reconciling = reconcilingVisit === visit

  const {
    activeJob,
    errorJobs,
    successfulJobs,
    userJobCount,
    setJobVersion,
  } = useAskJobs({
    userId,
    visit,
    activeId,
    isCurrentView,
    viewRef,
    loadConversations,
    loadDetail,
    detailReady,
    currentDetailError,
    reconciling,
    restoredDrafts,
    recoveredJobKey,
    setQuestion,
  })

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
  const retryCandidate = errorJobs.find((job) => job.text === question.trim() &&
    (activeId ? job.targetId === activeId : job.targetId === null))
  const capacityBlocked = userJobCount >= MAX_CHAT_JOBS && !retryCandidate
  const busy = !detailReady || reconciling || asking || Boolean(pendingMessage) || capacityBlocked
  const shownSelectedKbs = detailReady ? selectedKbs : []
  const displayedModelId = selectedModelId ?? modelDefaultId
  const askError = validationError?.visit === visit ? validationError.text : ''

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
      key: newJobKey(), userId, session, visit, intent: originIntent, targetId: activeId, text,
      kbIds: [...selectedKbs], baselineId: Math.max(0, ...messages.map((message) => message.id)),
      initialDetail: loadedDetail ? { ...loadedDetail, messages } : null,
      phase: activeId ? 'asking' : 'creating',
    }
    askJobs.set(job.key, job)
    publishJob('change', job)

    try {
      // 提交前刷新默认模型，并把当前显示的模型固定到这一轮请求。
      // 若刷新失败，保留已显示的默认模型；从未加载成功则由服务端选择。
      let requestModelId = selectedModelId
      if (requestModelId === null) {
        // 聚焦时可能已有一次旧请求；等它结束后再获取提交时的最新默认值。
        let fallbackDefaultId = modelDefaultId
        const pending = modelRequest.current
        if (pending && pending.session === session && pending.userId === userId) {
          const previous = await pending.promise
          if (previous) fallbackDefaultId = previous.default_id
        }
        if (session === getToken()) {
          const refreshed = await loadModels()
          requestModelId = refreshed ? refreshed.default_id : fallbackDefaultId
        }
      }
      if (session !== getToken()) {
        job.error = SESSION_SIGNED_OUT
        job.ambiguous = false
        job.phase = 'error'
        publishJob('change', job)
        return
      }
      if (!job.targetId) {
        const title = text.slice(0, 28) + (text.length > 28 ? '…' : '')
        const created = await chatApi.create(title, job.kbIds)
        if (askJobs.get(job.key) !== job) return
        job.targetId = created.id
        job.initialDetail = { ...created, messages: [] }
        if (session !== getToken()) {
          job.error = SESSION_SIGNED_OUT
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
        job.error = SESSION_SIGNED_OUT
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
      }, undefined, requestModelId)
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
      job.ambiguous = job.phase === 'asking' && !(err instanceof ApiError && [400, 403, 404, 422].includes(err.status))
      job.phase = 'error'
      // 模型被删除或停用时，服务端在写入前返回 400；更新列表供用户重试。
      if (sameSession && err instanceof ApiError && err.status === 400) void loadModels()
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

  const selectConversation = (conv: Conversation) => {
    if (viewRef.current && activeId !== conv.id) viewRef.current.intent += 1
    if (sidebarOpen) closeSidebar()
  }

  const startRename = (conv: Conversation) => {
    setRenameTarget(conv)
    setRenameText(conv.title)
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
        <ConversationList
          conversations={conversations}
          activeId={activeId}
          convError={convError}
          convLoading={convLoading}
          convLoaded={convLoaded}
          pinnedCount={pinnedCount}
          pinBusyId={pinBusyId}
          sidebarOpen={sidebarOpen}
          newConversationRef={newConversationRef}
          onNewConversation={() => void newConversation()}
          onClose={closeSidebar}
          onRetryConversations={() => void loadConversations()}
          onSelect={selectConversation}
          onRenameConversation={startRename}
          onDeleteConversation={setDeleteTarget}
          onTogglePin={(conv) => void togglePin(conv)}
        />

        <section className="chat-main">
          <ChatToolbar
            sidebarOpen={sidebarOpen}
            sidebarToggleRef={sidebarToggleRef}
            onToggleSidebar={() => (sidebarOpen ? closeSidebar() : setSidebarOpen(true))}
            kbs={visibleKbs}
            kbsLoading={kbsLoading}
            kbsError={kbsError}
            selectedKbs={shownSelectedKbs}
            detailReady={detailReady}
            onToggleKb={toggleKb}
            onClearKbSelection={clearKbSelection}
            modelOptions={modelOptions}
            modelDefaultId={modelDefaultId}
            modelsLoading={modelsLoading}
            modelsError={modelsError}
            displayedModelId={displayedModelId}
            onSelectedModelChange={setSelectedModelId}
            onRetryModels={() => void loadModels()}
          />

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
                <EmptyThread
                  selectedCount={shownSelectedKbs.length}
                  onPickQuestion={(q) => setQuestion(q)}
                />
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

          <ChatComposer
            askError={askError}
            capacityBlocked={capacityBlocked}
            errorJobs={errorJobs}
            activeId={activeId}
            question={question}
            busy={busy}
            asking={asking}
            pendingMessage={pendingMessage}
            reconciling={reconciling}
            selectedCount={shownSelectedKbs.length}
            visibleDetail={visibleDetail}
            onQuestionChange={(value) => {
              if (recoveredJobKey.current !== null) restoredDrafts.current.delete(recoveredJobKey.current)
              setQuestion(value)
            }}
            onSend={() => void ask()}
            onViewJob={(job) => {
              if (viewRef.current) viewRef.current.intent += 1
              navigate(`/chat/${job.targetId}`)
            }}
            onRestoreDraft={(job) => {
              restoredDrafts.current.add(job.key)
              recoveredJobKey.current = job.key
              setQuestion(job.text)
            }}
            onForgetJob={(job) => forgetJob(job)}
          />
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

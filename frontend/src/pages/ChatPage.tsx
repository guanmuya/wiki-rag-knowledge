import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useAuth, useToast } from '../auth'
import { chatApi, kbApi } from '../api'
import type {
  AskResponse,
  ChatMessage,
  Conversation,
  ConversationDetail,
  KnowledgeBase,
} from '../types'
import { errorText, formatMs, formatTime } from '../utils'
import { Button, ErrorState, Modal, SkeletonLines, Spinner, TextInput } from '../components/ui'
import { Markdown } from '../components/Markdown'
import { MessageInsights } from '../components/Evidence'

export default function ChatPage() {
  const { conversationId } = useParams<{ conversationId: string }>()
  const navigate = useNavigate()
  const { push } = useToast()
  const { user } = useAuth()

  const [kbs, setKbs] = useState<KnowledgeBase[]>([])
  const [kbsLoading, setKbsLoading] = useState(true)
  const [kbsError, setKbsError] = useState<unknown>(null)

  const [conversations, setConversations] = useState<Conversation[]>([])
  const [convLoading, setConvLoading] = useState(true)
  const [convError, setConvError] = useState<unknown>(null)

  const [detail, setDetail] = useState<ConversationDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<unknown>(null)

  const [selectedKbs, setSelectedKbs] = useState<number[]>([])
  const [question, setQuestion] = useState('')
  const [asking, setAsking] = useState(false)
  const [askError, setAskError] = useState('')
  const [pending, setPending] = useState<string | null>(null)

  const [renameTarget, setRenameTarget] = useState<Conversation | null>(null)
  const [renameText, setRenameText] = useState('')
  const [renameBusy, setRenameBusy] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<Conversation | null>(null)
  const [deleteBusy, setDeleteBusy] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)

  const bottomRef = useRef<HTMLDivElement>(null)
  const skipDetailRef = useRef<number | null>(null)
  const defaultsApplied = useRef(false)

  const activeId = conversationId ? Number(conversationId) : null

  const loadKbs = useCallback(async () => {
    setKbsLoading(true)
    try {
      const data = await kbApi.list()
      setKbs(data.items ?? [])
      setKbsError(null)
    } catch (err) {
      setKbsError(err)
    } finally {
      setKbsLoading(false)
    }
  }, [])

  const loadConversations = useCallback(async () => {
    setConvLoading(true)
    try {
      const data = await chatApi.conversations()
      setConversations(data.items ?? [])
      setConvError(null)
    } catch (err) {
      setConvError(err)
    } finally {
      setConvLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadKbs()
    void loadConversations()
  }, [loadKbs, loadConversations])

  const loadDetail = useCallback(async (id: number) => {
    if (skipDetailRef.current === id) {
      // 刚本地追加完问答，避免再用服务端快照覆盖本地消息
      skipDetailRef.current = null
      return
    }
    setDetailLoading(true)
    setDetailError(null)
    try {
      const data = await chatApi.get(id)
      setDetail(data)
      setSelectedKbs(data.kb_ids ?? [])
    } catch (err) {
      setDetailError(err)
      setDetail(null)
    } finally {
      setDetailLoading(false)
    }
  }, [])

  useEffect(() => {
    if (activeId && Number.isFinite(activeId)) {
      void loadDetail(activeId)
    } else {
      setDetail(null)
      if (!defaultsApplied.current && kbs.length > 0) {
        defaultsApplied.current = true
        setSelectedKbs(kbs.map((k) => k.id))
      }
    }
  }, [activeId, loadDetail, kbs])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [detail?.messages?.length, asking, pending])

  const toggleKb = (id: number) => {
    const next = selectedKbs.includes(id)
      ? selectedKbs.filter((x) => x !== id)
      : [...selectedKbs, id]
    setSelectedKbs(next)
    void persistKbSelection(next)
  }

  /** 把当前知识库选择写回会话（PATCH /conversations/{id}），失败只提示不回滚，避免操作抖动。 */
  const persistKbSelection = async (ids: number[]) => {
    if (!activeId || !Number.isFinite(activeId)) return
    try {
      await chatApi.update(activeId, { kb_ids: ids })
      setConversations((prev) =>
        prev.map((c) => (c.id === activeId ? { ...c, kb_ids: ids } : c)),
      )
      setDetail((prev) => (prev && prev.id === activeId ? { ...prev, kb_ids: ids } : prev))
    } catch (err) {
      push(`知识库选择未能保存：${errorText(err)}`, 'error')
    }
  }

  const clearKbSelection = () => {
    setSelectedKbs([])
    void persistKbSelection([])
  }

  const newConversation = async () => {
    try {
      const title = `新会话 ${new Date().toLocaleString('zh-CN', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
      })}`
      const created = await chatApi.create(title, selectedKbs)
      await loadConversations()
      setSidebarOpen(false)
      navigate(`/chat/${created.id}`)
    } catch (err) {
      push(errorText(err), 'error')
    }
  }

  const ask = async () => {
    const text = question.trim()
    if (!text) {
      setAskError('请输入问题')
      return
    }
    if (selectedKbs.length === 0) {
      setAskError('请至少选择一个知识库')
      return
    }
    setAskError('')
    setAsking(true)
    setPending(text)
    setQuestion('')

    let targetId = activeId
    try {
      if (!targetId) {
        const title = text.slice(0, 28) + (text.length > 28 ? '…' : '')
        const created = await chatApi.create(title, selectedKbs)
        targetId = created.id
        // 新会话：本地直接接上消息，避免路由切换后重新拉取造成闪烁
        skipDetailRef.current = created.id
        setDetail({
          id: created.id,
          title: created.title,
          kb_ids: selectedKbs,
          message_count: 0,
          created_at: created.created_at,
          updated_at: created.updated_at,
          messages: [],
        })
        void loadConversations()
        navigate(`/chat/${created.id}`, { replace: true })
      }

      const res: AskResponse = await chatApi.ask(targetId, text, selectedKbs)
      if (res.denied && res.denied.length > 0) {
        push(
          `以下知识库本次被跳过：${res.denied.map((d) => `${d.kb_name}（${d.reason}）`).join('；')}`,
          'error',
        )
      }
      setDetail((prev) => {
        const base: ConversationDetail =
          prev && prev.id === targetId
            ? prev
            : {
                id: targetId as number,
                title: text.slice(0, 28),
                kb_ids: selectedKbs,
                message_count: 0,
                created_at: new Date().toISOString(),
                updated_at: new Date().toISOString(),
                messages: [],
              }
        const userMsg: ChatMessage = {
          id: -Date.now(),
          role: 'user',
          content: text,
          kb_ids: selectedKbs,
          created_at: new Date().toISOString(),
        }
        const assistant: ChatMessage = {
          ...res.message,
          evidence: res.evidence,
          route: res.route,
          latency_ms: res.usage?.latency_ms ?? null,
          kb_ids: selectedKbs,
          insufficient: res.insufficient,
          usage: res.usage,
        }
        return {
          ...base,
          messages: [...base.messages, userMsg, assistant],
          message_count: base.messages.length + 2,
        }
      })
      setPending(null)
      void loadConversations()
    } catch (err) {
      setAskError(errorText(err))
      setPending(null)
      setQuestion(text)
    } finally {
      setAsking(false)
    }
  }

  const doRename = async () => {
    if (!renameTarget) return
    const title = renameText.trim()
    if (!title) return
    setRenameBusy(true)
    try {
      await chatApi.update(renameTarget.id, { title })
      await loadConversations()
      if (detail?.id === renameTarget.id) setDetail({ ...detail, title })
      setRenameTarget(null)
      push('会话已重命名', 'success')
    } catch (err) {
      push(errorText(err), 'error')
    } finally {
      setRenameBusy(false)
    }
  }

  const doDelete = async () => {
    if (!deleteTarget) return
    setDeleteBusy(true)
    try {
      await chatApi.remove(deleteTarget.id)
      push('会话已删除', 'success')
      const wasActive = detail?.id === deleteTarget.id
      setDeleteTarget(null)
      await loadConversations()
      if (wasActive) navigate('/chat', { replace: true })
    } catch (err) {
      push(errorText(err), 'error')
    } finally {
      setDeleteBusy(false)
    }
  }

  const openWikiPage = (kbId: number, slug: string) => {
    navigate(`/kbs/${kbId}?tab=wiki&slug=${encodeURIComponent(slug)}`)
  }

  const messages = detail?.messages ?? []
  const visibleKbs = kbs

  return (
    <>
      <div className="chat-shell">
        <aside className={`chat-sidebar ${sidebarOpen ? 'open' : ''}`}>
          <div className="chat-sidebar-head">
            <Button variant="primary" className="full" size="sm" onClick={() => void newConversation()}>
              + 新建会话
            </Button>
          </div>
          <div className="conv-list">
            {convLoading ? (
              <div style={{ padding: 8 }}>
                <SkeletonLines rows={4} />
              </div>
            ) : convError ? (
              <div className="tiny" style={{ padding: 10, color: 'var(--c-danger)' }}>
                会话列表加载失败
              </div>
            ) : conversations.length === 0 ? (
              <div className="tiny muted" style={{ padding: 12, textAlign: 'center' }}>
                还没有会话，点上方新建
              </div>
            ) : (
              conversations.map((c) => (
                <div
                  key={c.id}
                  className={`conv-item ${activeId === c.id ? 'active' : ''}`}
                  onClick={() => {
                    navigate(`/chat/${c.id}`)
                    setSidebarOpen(false)
                  }}
                >
                  <span className="conv-title" title={c.title}>
                    {c.title}
                  </span>
                  <span className="conv-actions">
                    <button
                      type="button"
                      title="重命名"
                      onClick={(e) => {
                        e.stopPropagation()
                        setRenameTarget(c)
                        setRenameText(c.title)
                      }}
                    >
                      ✎
                    </button>
                    <button
                      type="button"
                      title="删除"
                      onClick={(e) => {
                        e.stopPropagation()
                        setDeleteTarget(c)
                      }}
                    >
                      ×
                    </button>
                  </span>
                </div>
              ))
            )}
          </div>
        </aside>

        <section className="chat-main">
          <div className="chat-toolbar">
            <button
              type="button"
              className="icon-btn mobile-only"
              onClick={() => setSidebarOpen((v) => !v)}
              aria-label="会话列表"
            >
              ☰
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
                  <span
                    key={k.id}
                    className={`kb-pick ${selectedKbs.includes(k.id) ? 'on' : ''}`}
                    onClick={() => toggleKb(k.id)}
                    role="button"
                    tabIndex={0}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') toggleKb(k.id)
                    }}
                    title={`${k.name} · ${k.mode_label}`}
                  >
                    {k.name}
                    <span className="lv">{k.my_level === 'manage' ? '管理' : '只读'}</span>
                  </span>
                ))
              )}
            </div>
            <div className="spacer" />
            {selectedKbs.length > 0 ? (
              <button type="button" className="btn-link" onClick={clearKbSelection}>
                清空选择
              </button>
            ) : null}
          </div>

          <div className="chat-scroll">
            <div className="chat-thread">
              {detailLoading ? (
                <div className="card card-pad">
                  <SkeletonLines rows={5} />
                </div>
              ) : detailError ? (
                <ErrorState error={detailError} onRetry={() => activeId && void loadDetail(activeId)} />
              ) : messages.length === 0 && !pending ? (
                <div className="chat-empty">
                  <div className="chat-empty-icon">✦</div>
                  <div className="chat-empty-title">向知识库提问</div>
                  <div className="chat-empty-hint">
                    {selectedKbs.length === 0
                      ? '先在上方选择要检索的知识库，然后输入问题。'
                      : `已选择 ${selectedKbs.length} 个知识库。回答会附带证据原文、文档版本与页码，可点开核对。`}
                  </div>
                  {selectedKbs.length > 0 ? (
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
                  {messages.map((m) => (
                    <MessageBubble
                      key={m.id}
                      message={m}
                      onOpenWikiPage={openWikiPage}
                      userName={user?.display_name || user?.username || '我'}
                    />
                  ))}
                  {pending ? (
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
                    </>
                  ) : null}
                </>
              )}
              <div ref={bottomRef} />
            </div>
          </div>

          <div className="chat-composer">
            {askError ? (
              <div
                style={{
                  maxWidth: 900,
                  margin: '0 auto 8px',
                  color: 'var(--c-danger)',
                  fontSize: 12.5,
                }}
              >
                {askError}
              </div>
            ) : null}
            <div className="composer-inner">
              <textarea
                className="composer-input"
                aria-label="提问输入框"
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                placeholder={
                  selectedKbs.length === 0
                    ? '请先在上方选择知识库…'
                    : '输入你的问题，Enter 发送，Shift+Enter 换行'
                }
                disabled={asking}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault()
                    if (!asking) void ask()
                  }
                }}
              />
              <Button
                variant="primary"
                loading={asking}
                disabled={asking || !question.trim()}
                onClick={() => void ask()}
                style={{ height: 44, minWidth: 84 }}
              >
                发送
              </Button>
            </div>
            <div className="composer-hint">
              <span>已选 {selectedKbs.length} 个知识库</span>
              <span>回答基于检索到的证据，可能不完整，请核对来源</span>
              {detail ? <span>会话 #{detail.id}</span> : null}
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

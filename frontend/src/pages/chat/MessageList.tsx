import { useState } from 'react'
import { MessageInsights } from '../../components/Evidence'
import { Markdown } from '../../components/Markdown'
import { IconChevronDown, IconChevronRight, IconMessage } from '../../components/icons'
import { SkeletonLines, Spinner } from '../../components/ui'
import type { ChatMessage, ThinkingStream } from '../../types'
import { formatMs, formatTime } from '../../utils'

/** 空会话时的示例问题：只填进输入框，不直接发送，用户可以改。 */
const SAMPLE_QUESTIONS = [
  '出差住宿标准是多少？',
  '这个流程需要谁审批？',
  '产品的定价是怎么定的？',
]

/** 空会话占位：示例问题只填进输入框，不直接发送。 */
export function EmptyThread({
  selectedCount,
  onPickQuestion,
}: {
  selectedCount: number
  onPickQuestion: (question: string) => void
}) {
  return (
    <div className="chat-empty">
      <div className="chat-empty-icon"><IconMessage size={22} /></div>
      <div className="chat-empty-title">向知识库提问</div>
      <div className="chat-empty-hint">
        {selectedCount === 0
          ? '先在上方选择要检索的知识库，然后输入问题。'
          : `已选择 ${selectedCount} 个知识库。回答会附带证据原文、文档版本与页码，可点开核对。`}
      </div>
      {selectedCount > 0 ? (
        <div className="chat-samples">
          {SAMPLE_QUESTIONS.map((q) => (
            <button
              key={q}
              type="button"
              className="sample-chip"
              onClick={() => onPickQuestion(q)}
            >
              {q}
            </button>
          ))}
        </div>
      ) : null}
      <div className="chat-empty-tip">Enter 发送 · Shift+Enter 换行 · 回答基于检索到的证据</div>
    </div>
  )
}

/** 「正在检索并生成回答」占位：本页刚发出的提问与服务端 pending 消息共用同一套样式。 */
export function GeneratingBubble() {
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
export function StreamingBubble({ stream }: { stream: ThinkingStream }) {
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

export function MessageBubble({
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

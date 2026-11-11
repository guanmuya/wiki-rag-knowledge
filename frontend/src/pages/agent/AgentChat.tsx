import type { RefObject } from 'react'
import type {
  AgentCore,
  AgentMessage,
  AgentModelOption,
  AgentSessionDetail,
  KnowledgeBase,
  UsageInfo,
} from '../../types'
import { Button, Select, Spinner } from '../../components/ui'
import { IconAgent, IconPlug, IconStop, IconTerminal } from '../../components/icons'
import { EvidencePanel, UsageLine } from '../../components/Evidence'
import { Markdown } from '../../components/Markdown'
import { formatMs, formatTime } from '../../utils'
import { coreUsable, type LiveRun } from './constants'

export function AgentChat({
  active,
  activeCore,
  cores,
  compatibleModels,
  kbs,
  isBusy,
  live,
  draft,
  sending,
  scrollRef,
  textareaRef,
  onRename,
  onChangeCore,
  onChangeModel,
  onToggleKb,
  onOpenWorkspace,
  onStop,
  onOpenPlugins,
  onSend,
  onDraftChange,
}: {
  active: AgentSessionDetail
  activeCore: AgentCore | null
  cores: AgentCore[]
  compatibleModels: AgentModelOption[]
  kbs: KnowledgeBase[]
  isBusy: boolean
  live: LiveRun | null
  draft: string
  sending: boolean
  scrollRef: RefObject<HTMLDivElement>
  textareaRef: RefObject<HTMLTextAreaElement>
  onRename: () => void
  onChangeCore: (coreId: string) => void
  onChangeModel: (value: string) => void
  onToggleKb: (kbId: number) => void
  onOpenWorkspace: () => void
  onStop: () => void
  onOpenPlugins: () => void
  onSend: () => void
  onDraftChange: (value: string) => void
}) {
  const visibleMessages = (active?.messages ?? []).filter(
    (m) => !(live && m.role === 'assistant' && m.status === 'pending'),
  )
  const kbNames = active ? active.kb_ids
    .map((id) => kbs.find((k) => k.id === id)?.name ?? `#${id}`) : []

  return (
    <>
      <div className="chat-toolbar agent-toolbar">
        <button
          type="button"
          className="agent-title-btn"
          onClick={onRename}
          title="重命名会话"
        >
          <strong>{active.title}</strong>
        </button>
        <span className="agent-toolbar-divider" />
        <label className="agent-inline-select">
          <span className="tiny muted">核心</span>
          <Select
            value={active.core_id}
            onChange={(e) => onChangeCore(e.target.value)}
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
            onChange={(e) => onChangeModel(e.target.value)}
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
                    onChange={() => onToggleKb(kb.id)}
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
            onClick={onOpenWorkspace}
            title={active.workspace}
          >
            <IconTerminal size={13} />
            <span className="truncate">{active.workspace}</span>
          </button>
        ) : null}
        <span className="spacer" />
        {live ? (
          <Button variant="ghost" size="sm" onClick={onStop}>
            <IconStop size={13} /> 停止
          </Button>
        ) : null}
        <Button variant="ghost" size="sm" onClick={onOpenPlugins}>
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

          {live ? <LiveBubble live={live} core={activeCore} onStop={onStop} /> : null}
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
            onChange={(e) => onDraftChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault()
                onSend()
              }
            }}
          />
          <Button
            variant="primary"
            onClick={onSend}
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

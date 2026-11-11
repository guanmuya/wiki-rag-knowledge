import type { RefObject } from 'react'
import { IconMenu } from '../../components/icons'
import { Button, Select } from '../../components/ui'
import type { ChatMessage, ChatModelOption, ConversationDetail, KnowledgeBase } from '../../types'
import type { AskJob } from './jobs'

/** 顶部工具条：移动端会话列表开关、知识库选择、模型选择。 */
export function ChatToolbar({
  sidebarOpen,
  sidebarToggleRef,
  onToggleSidebar,
  kbs,
  kbsLoading,
  kbsError,
  selectedKbs,
  detailReady,
  onToggleKb,
  onClearKbSelection,
  modelOptions,
  modelDefaultId,
  modelsLoading,
  modelsError,
  displayedModelId,
  onSelectedModelChange,
  onRetryModels,
}: {
  sidebarOpen: boolean
  sidebarToggleRef: RefObject<HTMLButtonElement>
  onToggleSidebar: () => void
  kbs: KnowledgeBase[]
  kbsLoading: boolean
  kbsError: unknown
  selectedKbs: number[]
  detailReady: boolean
  onToggleKb: (id: number) => void
  onClearKbSelection: () => void
  modelOptions: ChatModelOption[]
  modelDefaultId: number | null
  modelsLoading: boolean
  modelsError: unknown
  displayedModelId: number | null
  onSelectedModelChange: (id: number | null) => void
  onRetryModels: () => void
}) {
  return (
    <div className="chat-toolbar">
      <button
        type="button"
        className="icon-btn mobile-only"
        ref={sidebarToggleRef}
        onClick={onToggleSidebar}
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
        ) : kbs.length === 0 ? (
          <span className="tiny muted">暂无可选知识库，请联系管理员授权</span>
        ) : (
          kbs.map((k) => (
            <button
              key={k.id}
              type="button"
              className={`kb-pick ${selectedKbs.includes(k.id) ? 'on' : ''}`}
              aria-pressed={selectedKbs.includes(k.id)}
              disabled={!detailReady}
              onClick={() => onToggleKb(k.id)}
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
          value={displayedModelId ?? ''}
          onChange={(e) => {
            const id = e.target.value ? Number(e.target.value) : null
            onSelectedModelChange(id === modelDefaultId ? null : id)
          }}
          disabled={modelsLoading || modelOptions.length === 0}
          aria-label="选择回答使用的模型"
          title="选择回答使用的模型"
        >
          {modelOptions.length === 0 ? (
            <option value="">{modelsLoading ? '加载中…' : modelsError ? '模型加载失败' : '默认模型'}</option>
          ) : modelOptions.map((m) => (
            <option key={m.id} value={m.id}>
              {m.display}{m.id === modelDefaultId ? '（默认）' : ''}
            </option>
          ))}
        </Select>
      </label>
      {modelsError ? <button type="button" className="btn-link" onClick={onRetryModels}>重试模型列表</button> : null}
      {selectedKbs.length > 0 ? (
        <button type="button" className="btn-link" onClick={onClearKbSelection}>
          清空选择
        </button>
      ) : null}
    </div>
  )
}

/** 输入区：待处理提问清单、提问输入框、发送按钮与提示行。 */
export function ChatComposer({
  askError,
  capacityBlocked,
  errorJobs,
  activeId,
  question,
  busy,
  asking,
  pendingMessage,
  reconciling,
  selectedCount,
  visibleDetail,
  onQuestionChange,
  onSend,
  onViewJob,
  onRestoreDraft,
  onForgetJob,
}: {
  askError: string
  capacityBlocked: boolean
  errorJobs: AskJob[]
  activeId: number | null
  question: string
  busy: boolean
  asking: boolean
  pendingMessage: ChatMessage | null
  reconciling: boolean
  selectedCount: number
  visibleDetail: ConversationDetail | null
  onQuestionChange: (value: string) => void
  onSend: () => void
  onViewJob: (job: AskJob) => void
  onRestoreDraft: (job: AskJob) => void
  onForgetJob: (job: AskJob) => void
}) {
  return (
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
                      onClick={() => onViewJob(job)}>查看会话</button>
                  ) : (
                    <button type="button" className="btn-link" aria-label={`恢复未发送的问题：${job.text}`}
                      onClick={() => onRestoreDraft(job)}>恢复未发送的问题</button>
                  )}
                  <button type="button" className="btn-link" aria-label={`忽略这条提问：${job.text}`}
                    onClick={() => onForgetJob(job)}>忽略这条提问</button>
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
          onChange={(e) => onQuestionChange(e.target.value)}
          placeholder={
            selectedCount === 0
              ? '请先在上方选择知识库…'
              : '输入你的问题，Enter 发送，Shift+Enter 换行'
          }
          disabled={busy}
          onKeyDown={(e) => {
            // 229 是部分输入法在组合文字时使用的键码。
            const composing = e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229
            if (e.key === 'Enter' && !e.shiftKey && !composing) {
              e.preventDefault()
              if (!busy) onSend()
            }
          }}
        />
        <Button
          variant="primary"
          loading={asking || Boolean(pendingMessage)}
          disabled={busy || !question.trim()}
          onClick={onSend}
          style={{ height: 44, minWidth: 84 }}
        >
          发送
        </Button>
      </div>
      <div className="composer-hint">
        {reconciling ? <span>正在核对本次提问…</span> : null}
        <span>已选 {selectedCount} 个知识库</span>
        <span>回答基于检索到的证据，可能不完整，请核对来源</span>
        {visibleDetail ? <span>会话 #{visibleDetail.id}</span> : null}
      </div>
    </div>
  )
}

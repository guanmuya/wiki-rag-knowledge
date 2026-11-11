import { Fragment } from 'react'
import type { RefObject } from 'react'
import { Link } from 'react-router-dom'
import { IconClose } from '../../components/icons'
import { SkeletonLines } from '../../components/ui'
import type { Conversation } from '../../types'
import { parseApiTime } from '../../utils'

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

/** 左侧会话列表：置顶分组、时间标签与置顶/重命名/删除操作。 */
export function ConversationList({
  conversations,
  activeId,
  convError,
  convLoading,
  convLoaded,
  pinnedCount,
  pinBusyId,
  sidebarOpen,
  newConversationRef,
  onNewConversation,
  onClose,
  onRetryConversations,
  onSelect,
  onRenameConversation,
  onDeleteConversation,
  onTogglePin,
}: {
  conversations: Conversation[]
  activeId: number | null
  convError: unknown
  convLoading: boolean
  convLoaded: boolean
  pinnedCount: number
  pinBusyId: number | null
  sidebarOpen: boolean
  newConversationRef: RefObject<HTMLButtonElement>
  onNewConversation: () => void
  onClose: () => void
  onRetryConversations: () => void
  onSelect: (conv: Conversation) => void
  onRenameConversation: (conv: Conversation) => void
  onDeleteConversation: (conv: Conversation) => void
  onTogglePin: (conv: Conversation) => void
}) {
  return (
    <aside
      id="chat-conversation-sidebar"
      className={`chat-sidebar ${sidebarOpen ? 'open' : ''}`}
      onKeyDown={(e) => {
        if (e.key === 'Escape' && sidebarOpen) onClose()
      }}
    >
      <div className="chat-sidebar-head">
        <button
          type="button"
          ref={newConversationRef}
          className="btn btn-primary btn-sm full"
          onClick={onNewConversation}
        >
          + 新建会话
        </button>
        <button
          type="button"
          className="icon-btn mobile-only chat-sidebar-close"
          onClick={onClose}
          aria-label="关闭会话列表"
        >
          <IconClose size={16} />
        </button>
      </div>
      <div className="conv-list chat-conv-list">
        {convError ? (
          <div className="tiny" style={{ padding: 10, color: 'var(--c-danger)' }}>
            会话列表加载失败 <button type="button" className="btn-link" onClick={onRetryConversations}>重试会话列表</button>
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
                  onClick={() => onSelect(c)}
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
                    onClick={() => onTogglePin(c)}
                  >
                    <PinIcon filled={c.pinned} />
                  </button>
                  <button
                    type="button"
                    title="重命名"
                    aria-label={`重命名会话：${c.title}`}
                    onClick={() => onRenameConversation(c)}
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
                    onClick={() => onDeleteConversation(c)}
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
  )
}

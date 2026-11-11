import type { AgentCore, AgentSession } from '../../types'
import { Badge } from '../../components/ui'
import { IconAgent, IconPlug, IconPlus } from '../../components/icons'

export function SessionList({
  sessions,
  activeSessionId,
  activeCore,
  cores,
  modelLabel,
  canCreate,
  onNew,
  onOpenPlugins,
  onOpenSession,
  onDelete,
}: {
  sessions: AgentSession[]
  activeSessionId: number | null
  activeCore: AgentCore | null
  cores: AgentCore[]
  modelLabel: string
  canCreate: boolean
  onNew: () => void
  onOpenPlugins: () => void
  onOpenSession: (id: number) => void
  onDelete: (session: AgentSession) => void
}) {
  // 会话用的核心可能已经不在清单里（旧版内置检索核心、或插件被删掉）：
  // 显示「核心不可用」而不是照搬服务端的旧名字，避免历史会话再把旧入口带回页面上。
  const coreLabel = (session: AgentSession): string =>
    cores.find((core) => core.id === session.core_id)?.name ?? '核心不可用'

  return (
    <aside className="chat-sidebar agent-rail">
      <div className="chat-sidebar-head">
        <span className="agent-brand"><IconAgent size={16} /> 个人 Agent</span>
        <span className="spacer" />
        <button
          type="button"
          className="icon-btn"
          title="核心与插件"
          aria-label="核心与插件"
          onClick={onOpenPlugins}
        >
          <IconPlug size={16} />
        </button>
        <button
          type="button"
          className="btn btn-primary btn-sm"
          onClick={onNew}
          disabled={!canCreate}
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
              <Badge tone="info">{activeCore.version || '已安装'}</Badge>
            </div>
            <div className="tiny muted truncate">
              {modelLabel || '跟随应用默认模型'}
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
          <div key={s.id} className={`conv-item ${activeSessionId === s.id ? 'active' : ''}`}>
            <button
              type="button"
              className="conv-title agent-session-title"
              onClick={() => onOpenSession(s.id)}
              title={`${coreLabel(s)} · ${s.model_label || '默认模型'}`}
            >
              <span className="truncate">{s.title}</span>
              <span className="agent-session-sub">{coreLabel(s)}</span>
            </button>
            <span className="conv-actions">
              <button
                type="button"
                title="删除会话"
                aria-label={`删除会话 ${s.title}`}
                onClick={() => onDelete(s)}
              >
                ×
              </button>
            </span>
          </div>
        ))}
      </div>

      <div className="agent-rail-foot">
        <button type="button" className="btn btn-ghost btn-sm full" onClick={onOpenPlugins}>
          <IconPlug size={14} /> 核心与插件
        </button>
        <div className="tiny muted agent-foot-hint">
          CLI 核心以本机命令运行，仅在管理员账号下可用。
        </div>
      </div>
    </aside>
  )
}

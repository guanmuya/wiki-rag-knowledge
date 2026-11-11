import type { EmbeddingStatus, ModelConfig } from '../../types'
import { IconAlert } from '../../components/icons'
import { Badge, Button, ErrorState, Field, Select, Spinner } from '../../components/ui'
import type { EmbedKbState } from './constants'

/** 各知识库的向量签名一致性，由 Embedding 状态与知识库列表在页面上拼出来。 */
export interface EmbedKbView {
  kb_id: number
  kb_name: string
  embed_signature: string
  embed_dim: number
  chunk_count?: number
  state: EmbedKbState
}

export function EmbeddingCard({
  embedLoading,
  embedError,
  embedStatus,
  currentEmbedId,
  currentEmbedName,
  embedKbs,
  inconsistent,
  unknownConsistency,
  hasIndexedKb,
  embeddingModels,
  onRefresh,
  onPickTarget,
}: {
  embedLoading: boolean
  embedError: unknown
  embedStatus: EmbeddingStatus | null
  currentEmbedId: number | null
  currentEmbedName: string
  embedKbs: EmbedKbView[]
  inconsistent: boolean
  unknownConsistency: boolean
  hasIndexedKb: boolean
  embeddingModels: ModelConfig[]
  onRefresh: () => void
  onPickTarget: (model: ModelConfig) => void
}) {
  return (
    <div className="card">
      <div className="card-head">
        <h3>Embedding 切换</h3>
        <div className="spacer" />
        <Button size="sm" variant="ghost" onClick={onRefresh}>
          刷新
        </Button>
      </div>
      <div className="card-body">
        {embedLoading ? (
          <div className="state-block inline">
            <Spinner size={14} /> 状态加载中…
          </div>
        ) : embedError ? (
          <ErrorState error={embedError} onRetry={onRefresh} />
        ) : (
          <>
            <dl className="info-list">
              <dt>当前 Embedding</dt>
              <dd>
                {currentEmbedName || <span className="muted">未配置</span>}
                {embedStatus?.current_provider ? (
                  <span className="tiny muted"> · {embedStatus.current_provider}</span>
                ) : null}
                {embedStatus?.active?.offline ? <span className="tiny muted"> · 离线</span> : null}
              </dd>
              <dt>当前签名</dt>
              <dd>
                {embedStatus?.current_signature ? (
                  <span className="sig-box">{embedStatus.current_signature}</span>
                ) : (
                  <span className="muted">—</span>
                )}
              </dd>
              <dt>各库一致性</dt>
              <dd>
                {inconsistent ? (
                  <Badge tone="warn">存在签名不一致的知识库</Badge>
                ) : unknownConsistency ? (
                  <Badge tone="muted">无法判断</Badge>
                ) : hasIndexedKb ? (
                  <Badge tone="success">一致</Badge>
                ) : (
                  <Badge tone="muted">暂无索引</Badge>
                )}
              </dd>
              <dt>重建状态</dt>
              <dd>
                {embedStatus?.rebuilding ? (
                  <Badge tone="warn">全量重建进行中</Badge>
                ) : (
                  <span className="muted">无进行中的重建任务</span>
                )}
              </dd>
            </dl>

            {embedKbs.length > 0 ? (
              <div className="embed-status-grid">
                {embedKbs.map((kb) => (
                  <div className={`embed-kb ${kb.state === 'mismatch' ? 'bad' : ''}`} key={kb.kb_id}>
                    <div className="row" style={{ gap: 6 }}>
                      <span className="truncate strong" style={{ fontSize: 12.5 }}>
                        {kb.kb_name}
                      </span>
                      <span className="spacer" />
                      <Badge tone={kb.state === 'consistent' ? 'success' : kb.state === 'mismatch' ? 'warn' : 'muted'}>
                        {kb.state === 'unindexed' ? '无索引' : kb.state === 'unknown' ? '无法判断' : kb.state === 'consistent' ? '一致' : '需重建'}
                      </Badge>
                    </div>
                    <div className="mono tiny muted" style={{ marginTop: 4 }}>
                      {kb.state === 'unindexed' ? '未建立索引' : kb.embed_signature || '签名未知'} · {kb.embed_dim || 0} 维
                    </div>
                  </div>
                ))}
              </div>
            ) : null}

            <div className="divider" />
            <Field
              label="切换 Embedding 模型"
              hint="切换后已有索引的向量维度不兼容，需要全量重建所有知识库的分块索引。"
            >
              <div className="row row-wrap">
                <Select
                  value={String(currentEmbedId ?? '')}
                  onChange={(e) => {
                    const target = embeddingModels.find((m) => String(m.id) === e.target.value)
                    if (target) onPickTarget(target)
                  }}
                  style={{ maxWidth: 320 }}
                >
                  <option value={String(currentEmbedId ?? '')}>
                    {currentEmbedName
                      ? `${currentEmbedName}（当前${embedStatus?.active?.offline ? ' · 离线' : ''}）`
                      : '选择向量模型'}
                  </option>
                  {embeddingModels
                    .filter((m) => m.id !== currentEmbedId)
                    .map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.label || m.model} · {m.provider_name} · {m.embed_dim || '?'} 维
                      </option>
                    ))}
                </Select>
              </div>
            </Field>

            {inconsistent ? (
              <div className="insufficient-banner">
                <span className="banner-icon"><IconAlert size={15} /></span>
                <div style={{ flex: 1 }}>
                  检测到部分知识库的向量签名与当前 Embedding 不一致，这些库的向量检索可能失效。
                  建议切换到目标模型时执行全量重建。
                </div>
              </div>
            ) : null}
          </>
        )}
      </div>
    </div>
  )
}

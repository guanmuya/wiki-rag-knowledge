import { useEffect, useState } from 'react'
import type { EvidenceItem, KbRoute, RouteInfo, UsageInfo } from '../types'
import { formatMs, routeUsedLabel, type BadgeTone } from '../utils'
import { Badge } from './ui'

export function InsufficientNotice({ text }: { text?: string }) {
  return (
    <div className="insufficient-banner">
      <span style={{ fontSize: 15, lineHeight: 1.3 }}>⚠</span>
      <div style={{ flex: 1 }}>
        <strong>证据不足</strong>
        <div style={{ marginTop: 2 }}>
          {text ||
            '检索到的片段不足以支撑可靠回答，请补充文档、换个问法，或扩大知识库范围后重试。'}
        </div>
      </div>
    </div>
  )
}

export function RoutePanel({ route }: { route: RouteInfo }) {
  const routes = route?.kb_routes ?? []
  const stats = route?.stats
  return (
    <div className="route-panel">
      <div className="tiny strong" style={{ marginBottom: 4 }}>
        路由信息
      </div>
      {routes.length === 0 ? (
        <div className="muted tiny">未记录路由</div>
      ) : (
        routes.map((r) => <RouteRow key={r.kb_id} route={r} />)
      )}
      {stats ? (
        <div className="route-stats">
          <span>候选 {stats.candidate_total}</span>
          <span>入选 {stats.final_total}</span>
          <span>向量检索 {stats.embedding_ok ? '正常' : '不可用'}</span>
          <span>RRF k={stats.rrf_k}</span>
        </div>
      ) : null}
    </div>
  )
}

function RouteRow({ route }: { route: KbRoute }) {
  const tone: BadgeTone =
    route.used === 'wiki' ? 'info' : route.used === 'chunk' ? 'success' : 'muted'
  return (
    <div className="route-row">
      <span className="strong" style={{ fontSize: 12 }}>
        {route.kb_name}
      </span>
      <Badge tone={tone}>{routeUsedLabel(route.used)}</Badge>
      {route.fallback ? <Badge tone="warn">已回退</Badge> : null}
      <span className="muted" style={{ flex: 1, minWidth: 0 }}>
        {route.reason}
      </span>
      <span className="tiny muted mono">
        关键词 {route.keyword_hits} · 向量 {route.vector_hits} · Wiki {route.wiki_hits}
      </span>
    </div>
  )
}

export function UsageLine({ usage }: { usage: UsageInfo | null | undefined }) {
  if (!usage) return null
  const offline = usage.mode === 'offline' || usage.calls === 0
  return (
    <span className="tiny muted">
      {usage.latency_ms > 0 ? `耗时 ${formatMs(usage.latency_ms)} · ` : ''}
      {offline ? '离线抽取模式（未调用生成模型）' : `模型调用 ${usage.calls} 次`}
      {usage.prompt_tokens || usage.completion_tokens
        ? ` · 输入 ${usage.prompt_tokens} / 输出 ${usage.completion_tokens} tokens`
        : ''}
    </span>
  )
}

export function EvidencePanel({
  evidence,
  onOpenWikiPage,
}: {
  evidence: EvidenceItem[]
  onOpenWikiPage?: (kbId: number, slug: string) => void
}) {
  if (evidence.length === 0) {
    return <div className="evidence-panel" style={{ padding: '10px 13px' }}>
      <span className="muted small">本次没有返回证据片段。</span>
    </div>
  }
  return (
    <div className="evidence-panel">
      {evidence.map((ev) => (
        <EvidenceCard key={ev.evidence_id} item={ev} onOpenWikiPage={onOpenWikiPage} />
      ))}
    </div>
  )
}

function EvidenceCard({
  item,
  onOpenWikiPage,
}: {
  item: EvidenceItem
  onOpenWikiPage?: (kbId: number, slug: string) => void
}) {
  const [open, setOpen] = useState(false)
  const isWiki = item.source_type === 'wiki'
  const channels = Object.entries(item.channels ?? {})

  return (
    <div className="evidence-card">
      <div className="evidence-card-head">
        <span className="evidence-src">{item.kb_name}</span>
        <Badge tone={isWiki ? 'info' : 'muted'}>{isWiki ? 'Wiki' : '分块'}</Badge>
        <span className="muted">#{item.rank}</span>
        {item.doc_name ? <span className="muted">· {item.doc_name}</span> : null}
        {item.version_no ? <span className="muted">v{item.version_no}</span> : null}
        {item.locator ? <span className="muted">· {item.locator}</span> : null}
        {item.heading ? <span className="muted">· {item.heading}</span> : null}
        <span className="spacer" />
        <span className="tiny muted mono">score {item.score?.toFixed(3)}</span>
        {isWiki && item.page_slug && onOpenWikiPage ? (
          <button
            type="button"
            className="btn-link"
            onClick={() => onOpenWikiPage(item.kb_id, item.page_slug as string)}
          >
            查看 Wiki 页面
          </button>
        ) : null}
      </div>

      {isWiki && item.page_title ? (
        <div className="tiny" style={{ color: 'var(--c-primary)', marginBottom: 4 }}>
          页面：{item.page_title}
        </div>
      ) : null}

      <div className={`evidence-text ${open ? 'open' : ''}`}>{item.text}</div>
      {item.text && item.text.length > 120 ? (
        <button type="button" className="btn-link" onClick={() => setOpen((v) => !v)}>
          {open ? '收起原文' : '展开原文'}
        </button>
      ) : null}

      {item.evidence && item.evidence.length > 0 ? (
        <div style={{ marginTop: 6 }}>
          {item.evidence.map((e, i) => (
            <div key={i} className="evidence-quote">
              <div className="tiny muted">
                {e.doc_name}
                {e.version_no ? ` v${e.version_no}` : ''}
                {e.locator ? ` · ${e.locator}` : ''}
              </div>
              {e.quote}
            </div>
          ))}
        </div>
      ) : null}

      {channels.length > 0 ? (
        <div className="evidence-channels">
          {channels.map(([k, v]) => (
            <span key={k}>
              {k}: {typeof v === 'number' ? v.toFixed(3).replace(/\.?0+$/, '') : String(v)}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  )
}

/** 问答消息里的证据/路由折叠区。 */
export function MessageInsights({
  evidence,
  route,
  usage,
  latencyMs,
  insufficient,
  onOpenWikiPage,
}: {
  evidence: EvidenceItem[]
  route: RouteInfo | null | undefined
  usage?: UsageInfo | null
  latencyMs?: number | null
  insufficient?: boolean
  onOpenWikiPage?: (kbId: number, slug: string) => void
}) {
  const [open, setOpen] = useState(false)
  const count = evidence?.length ?? 0

  useEffect(() => {
    setOpen(false)
  }, [evidence])

  return (
    <div>
      {insufficient ? <InsufficientNotice /> : null}
      <div className="row row-wrap" style={{ gap: 12 }}>
        <button type="button" className="evidence-toggle" onClick={() => setOpen((v) => !v)}>
          {open ? '▾' : '▸'} 证据 {count} 条
        </button>
        {route?.kb_routes?.length ? (
          <button type="button" className="evidence-toggle" onClick={() => setOpen((v) => !v)}>
            路由 {route.kb_routes.length} 个库
          </button>
        ) : null}
        {usage ? <UsageLine usage={usage} /> : null}
        {!usage && latencyMs ? (
          <span className="tiny muted">耗时 {formatMs(latencyMs)}</span>
        ) : null}
      </div>

      {open ? (
        <>
          {count > 0 ? (
            <EvidencePanel evidence={evidence} onOpenWikiPage={onOpenWikiPage} />
          ) : (
            <div className="evidence-panel" style={{ padding: '10px 13px' }}>
              <span className="muted small">本次没有返回证据片段。</span>
            </div>
          )}
          {route ? <RoutePanel route={route} /> : null}
        </>
      ) : null}
    </div>
  )
}

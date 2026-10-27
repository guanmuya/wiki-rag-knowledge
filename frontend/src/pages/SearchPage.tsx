import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useToast } from '../auth'
import { chatApi, kbApi } from '../api'
import type { EvidenceItem, KbRoute, KnowledgeBase, RouteStats, SearchPreviewResponse } from '../types'
import { errorText, formatMs, routeUsedLabel } from '../utils'
import { Badge, Button, EmptyState, ErrorState, SkeletonLines, Spinner, TextInput } from '../components/ui'

/**
 * 检索调试：不生成回答，只跑检索管线，把候选、覆盖率、排序与路由全部摊开。
 * 论文实验主要看：coverage（跨库可比）、channels（通道名次）、score（融合分）、路由 used/fallback。
 */
export default function SearchPage() {
  const { push } = useToast()
  const navigate = useNavigate()

  const [kbs, setKbs] = useState<KnowledgeBase[]>([])
  const [kbsLoading, setKbsLoading] = useState(true)
  const [kbsError, setKbsError] = useState<unknown>(null)
  const [selected, setSelected] = useState<number[]>([])

  const [query, setQuery] = useState('')
  const [result, setResult] = useState<SearchPreviewResponse | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [elapsed, setElapsed] = useState<number | null>(null)
  const [showContext, setShowContext] = useState(false)

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

  useEffect(() => {
    void loadKbs()
  }, [loadKbs])

  useEffect(() => {
    // 首次加载后默认全选，便于一次跑通
    if (kbs.length > 0 && selected.length === 0) {
      setSelected(kbs.map((k) => k.id))
    }
  }, [kbs, selected.length])

  const toggle = (id: number) => {
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]))
  }

  const run = async () => {
    const q = query.trim()
    if (!q) {
      setError('请输入查询语句')
      return
    }
    if (selected.length === 0) {
      setError('请至少选择一个知识库')
      return
    }
    setError('')
    setBusy(true)
    const started = performance.now()
    try {
      const res = await chatApi.preview(selected, q)
      setResult(res)
      setElapsed(performance.now() - started)
      if (res.denied && res.denied.length > 0) {
        push(
          `以下知识库被跳过：${res.denied.map((d) => `${d.kb_name}（${d.reason}）`).join('；')}`,
          'error',
        )
      }
    } catch (err) {
      setError(errorText(err))
      setResult(null)
    } finally {
      setBusy(false)
    }
  }

  const openWikiPage = (kbId: number, slug: string) => {
    navigate(`/kbs/${kbId}?tab=wiki&slug=${encodeURIComponent(slug)}`)
  }

  const stats = result?.stats ?? null
  const routes = result?.routes?.kb_routes ?? []
  const evidence = result?.evidence ?? []
  const avgCoverage = useMemo(() => {
    if (evidence.length === 0) return 0
    const sum = evidence.reduce((acc, e) => acc + (e.coverage ?? 0), 0)
    return sum / evidence.length
  }, [evidence])

  return (
    <>
      <div className="page-head">
        <div>
          <h1>检索调试</h1>
          <div className="sub">
            只跑检索、不生成回答：观察候选数量、查询词覆盖率、通道名次与路由决策，用于调参与实验复现。
          </div>
        </div>
        <div className="spacer" />
        <div className="page-head-actions">
          <Button variant="ghost" onClick={() => void loadKbs()}>
            刷新知识库
          </Button>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-body">
          {kbsLoading ? (
            <div className="state-block inline">
              <Spinner size={14} /> 知识库加载中…
            </div>
          ) : kbsError ? (
            <ErrorState error={kbsError} onRetry={() => void loadKbs()} />
          ) : kbs.length === 0 ? (
            <EmptyState title="没有可检索的知识库" hint="请联系管理员为你授权知识库访问权限。" />
          ) : (
            <>
              <div className="field">
                <span className="field-label">检索范围</span>
                <div className="kb-selector">
                  {kbs.map((k) => (
                    <span
                      key={k.id}
                      className={`kb-pick ${selected.includes(k.id) ? 'on' : ''}`}
                      onClick={() => toggle(k.id)}
                      role="button"
                      tabIndex={0}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' || e.key === ' ') toggle(k.id)
                      }}
                      title={`${k.name} · ${k.mode_label}`}
                    >
                      {k.name}
                      <span className="lv">{k.mode === 'wiki' ? 'wiki' : 'basic'}</span>
                    </span>
                  ))}
                  <button
                    type="button"
                    className="btn-link"
                    onClick={() =>
                      setSelected(selected.length === kbs.length ? [] : kbs.map((k) => k.id))
                    }
                  >
                    {selected.length === kbs.length ? '取消全选' : '全选'}
                  </button>
                </div>
              </div>

              <div className="row row-wrap" style={{ gap: 10, alignItems: 'flex-end' }}>
                <div style={{ flex: 1, minWidth: 260 }}>
                  <span className="field-label">查询语句</span>
                  <TextInput
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder="例如：试用期多久？转正流程是什么？"
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !busy) void run()
                    }}
                  />
                </div>
                <Button variant="primary" loading={busy} onClick={() => void run()}>
                  运行检索
                </Button>
              </div>
              {error ? (
                <div className="field-error" style={{ marginTop: 8 }}>
                  {error}
                </div>
              ) : null}
              <div className="tiny muted" style={{ marginTop: 8 }}>
                已选 {selected.length} 个知识库 · 等价于 POST /api/search/preview，不写会话、不调用生成模型
              </div>
            </>
          )}
        </div>
      </div>

      {busy ? (
        <div className="card card-pad">
          <div className="row" style={{ gap: 8, marginBottom: 10 }}>
            <Spinner size={14} />
            <span className="muted">正在检索…</span>
          </div>
          <SkeletonLines rows={4} />
        </div>
      ) : !result ? (
        <div className="card">
          <EmptyState
            title="还没有检索结果"
            hint="选择知识库并输入查询语句，运行后这里会展示证据列表与路由信息。"
          />
        </div>
      ) : (
        <div className="col" style={{ gap: 16 }}>
          <div className="card">
            <div className="card-head">
              <h3>检索概览</h3>
              <div className="spacer" />
              {elapsed !== null ? (
                <span className="tiny muted">往返 {formatMs(elapsed)}</span>
              ) : null}
            </div>
            <div className="card-body">
              {stats ? <StatsGrid stats={stats} avgCoverage={avgCoverage} count={evidence.length} /> : null}
              {stats?.embedding_ok === false ? (
                <div className="insufficient-banner" style={{ marginTop: 12 }}>
                  <span>⚠</span>
                  <div style={{ flex: 1 }}>
                    向量通道不可用{stats.embedding_error ? `：${stats.embedding_error}` : ''}，
                    本次结果仅由关键词通道产生。
                  </div>
                </div>
              ) : null}
              {stats?.kb_coverage && Object.keys(stats.kb_coverage).length > 0 ? (
                <div style={{ marginTop: 12 }}>
                  <div className="section-title">各库入选分布</div>
                  <div className="row row-wrap" style={{ gap: 8 }}>
                    {Object.entries(stats.kb_coverage).map(([name, n]) => (
                      <span key={name} className="coverage-chip">
                        {name}
                        <strong>{n}</strong>
                      </span>
                    ))}
                  </div>
                </div>
              ) : null}
              {stats?.query_terms && stats.query_terms.length > 0 ? (
                <div style={{ marginTop: 12 }}>
                  <div className="section-title">切分后的查询词</div>
                  <div className="row row-wrap" style={{ gap: 6 }}>
                    {stats.query_terms.map((t) => (
                      <span key={t} className="term-chip">
                        {t}
                      </span>
                    ))}
                  </div>
                </div>
              ) : null}
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <h3>路由信息</h3>
              <span className="tiny muted">{routes.length} 个库</span>
            </div>
            <div className="card-body card-body-flush">
              {routes.length === 0 ? (
                <EmptyState title="没有路由记录" />
              ) : (
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>知识库</th>
                        <th>模式</th>
                        <th>命中通道</th>
                        <th>是否回退</th>
                        <th>关键词 / 向量 / Wiki 命中</th>
                        <th>决策原因</th>
                      </tr>
                    </thead>
                    <tbody>
                      {routes.map((r) => (
                        <RouteRow key={r.kb_id} route={r} />
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <h3>证据列表</h3>
              <span className="tiny muted">{evidence.length} 条</span>
              <div className="spacer" />
              {result.context_preview ? (
                <Button size="sm" variant="ghost" onClick={() => setShowContext((v) => !v)}>
                  {showContext ? '隐藏上下文' : '查看拼装上下文'}
                </Button>
              ) : null}
            </div>
            <div className="card-body card-body-flush">
              {evidence.length === 0 ? (
                <EmptyState
                  title="没有命中任何证据"
                  hint="换一个查询词，或确认所选知识库已完成索引（文档状态为「可用」）。"
                />
              ) : (
                <div className="table-wrap">
                  <table className="table evidence-table">
                    <thead>
                      <tr>
                        <th style={{ width: 48 }}>#</th>
                        <th style={{ width: 130 }}>知识库</th>
                        <th style={{ width: 72 }}>类型</th>
                        <th style={{ width: 84 }}>覆盖率</th>
                        <th style={{ width: 84 }}>融合分</th>
                        <th style={{ width: 140 }}>通道名次</th>
                        <th>来源 / 位置</th>
                      </tr>
                    </thead>
                    <tbody>
                      {evidence.map((ev) => (
                        <EvidenceRow key={ev.evidence_id} item={ev} onOpenWikiPage={openWikiPage} />
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>

          {showContext && result.context_preview ? (
            <div className="card">
              <div className="card-head">
                <h3>拼装后的上下文</h3>
                <span className="tiny muted">{result.context_preview.length} 字符</span>
              </div>
              <div className="card-body">
                <pre className="context-preview">{result.context_preview}</pre>
              </div>
            </div>
          ) : null}
        </div>
      )}
    </>
  )
}

function StatsGrid({
  stats,
  avgCoverage,
  count,
}: {
  stats: RouteStats
  avgCoverage: number
  count: number
}) {
  return (
    <div className="kb-stats" style={{ paddingTop: 0, borderTop: 'none', gridTemplateColumns: 'repeat(6, 1fr)' }}>
      <div className="stat-cell">
        <span className="stat-value">{stats.candidate_total}</span>
        <span className="stat-label">候选数</span>
      </div>
      <div className="stat-cell">
        <span className="stat-value">{stats.final_total}</span>
        <span className="stat-label">入选数</span>
      </div>
      <div className="stat-cell">
        <span className="stat-value">{count}</span>
        <span className="stat-label">返回证据</span>
      </div>
      <div className="stat-cell">
        <span className="stat-value">{avgCoverage.toFixed(2)}</span>
        <span className="stat-label">平均覆盖率</span>
      </div>
      <div className="stat-cell">
        <span className="stat-value">{stats.rrf_k}</span>
        <span className="stat-label">RRF k</span>
      </div>
      <div className="stat-cell">
        <span className="stat-value">{stats.per_kb_limit ?? '—'}</span>
        <span className="stat-label">每库候选上限</span>
      </div>
    </div>
  )
}

function RouteRow({ route }: { route: KbRoute }) {
  const tone = route.used === 'wiki' ? 'info' : route.used === 'chunk' ? 'success' : 'muted'
  return (
    <tr>
      <td className="strong">{route.kb_name}</td>
      <td>
        <Badge tone={route.mode === 'wiki' ? 'info' : 'muted'}>
          {route.mode === 'wiki' ? 'Wiki' : '基础'}
        </Badge>
      </td>
      <td>
        <Badge tone={tone}>{routeUsedLabel(route.used)}</Badge>
      </td>
      <td>
        {route.fallback ? <Badge tone="warn">已回退</Badge> : <span className="muted tiny">否</span>}
      </td>
      <td className="mono small">
        {route.keyword_hits} / {route.vector_hits} / {route.wiki_hits}
      </td>
      <td className="small muted" style={{ wordBreak: 'break-word' }}>
        {route.reason}
      </td>
    </tr>
  )
}

function EvidenceRow({
  item,
  onOpenWikiPage,
}: {
  item: EvidenceItem
  onOpenWikiPage: (kbId: number, slug: string) => void
}) {
  const [open, setOpen] = useState(false)
  const coverage = item.coverage ?? 0
  const isWiki = item.source_type === 'wiki'
  const channels = Object.entries(item.channels ?? {})

  return (
    <>
      <tr>
        <td className="mono small">{item.rank}</td>
        <td className="small">{item.kb_name}</td>
        <td>
          <Badge tone={isWiki ? 'info' : 'muted'}>{isWiki ? 'Wiki' : '分块'}</Badge>
        </td>
        <td>
          <div className="coverage-bar" title={`查询词覆盖率 ${(coverage * 100).toFixed(1)}%`}>
            <div className="coverage-bar-fill" style={{ width: `${Math.min(100, coverage * 100)}%` }} />
            <span>{coverage.toFixed(3)}</span>
          </div>
        </td>
        <td className="mono small">{item.score?.toFixed(4)}</td>
        <td className="mono tiny muted">
          {channels.length === 0
            ? '—'
            : channels.map(([k, v]) => `${k}:${v}`).join(' ')}
        </td>
        <td className="small">
          <div className="truncate" style={{ maxWidth: 420 }} title={item.doc_name || item.page_title || ''}>
            {isWiki ? item.page_title || '（Wiki 页面）' : item.doc_name || '（未知文档）'}
            {item.version_no ? <span className="muted"> v{item.version_no}</span> : null}
          </div>
          <div className="tiny muted">
            {item.locator || '—'}
            {item.heading ? ` · ${item.heading}` : ''}
          </div>
          <div className="row" style={{ gap: 10, marginTop: 4 }}>
            <button type="button" className="btn-link" onClick={() => setOpen((v) => !v)}>
              {open ? '收起原文' : '查看原文'}
            </button>
            {isWiki && item.page_slug ? (
              <button
                type="button"
                className="btn-link"
                onClick={() => onOpenWikiPage(item.kb_id, item.page_slug as string)}
              >
                查看 Wiki 页面
              </button>
            ) : null}
          </div>
        </td>
      </tr>
      {open ? (
        <tr>
          <td />
          <td colSpan={6} style={{ background: 'var(--c-surface-alt)' }}>
            <div className="evidence-quote" style={{ maxHeight: 260, overflowY: 'auto' }}>
              {item.text}
            </div>
            {item.evidence && item.evidence.length > 0 ? (
              <div style={{ marginTop: 8 }}>
                <div className="tiny strong muted">原文依据</div>
                {item.evidence.map((e, i) => (
                  <div key={i} className="evidence-quote" style={{ marginTop: 4 }}>
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
          </td>
        </tr>
      ) : null}
    </>
  )
}

import { useCallback, useEffect, useMemo, useState } from 'react'
import { wikiApi } from '../api'
import type {
  KnowledgeBase,
  WikiPage,
  WikiPageDetail,
  WikiPageVersion,
} from '../types'
import { errorText, formatTime, taskStatusClass, wikiStatusLabel } from '../utils'
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  Modal,
  SkeletonLines,
  Spinner,
  TextArea,
} from '../components/ui'
import { Markdown } from './Markdown'

export default function WikiTab({
  kb,
  canManage,
  onToast,
  initialSlug,
}: {
  kb: KnowledgeBase
  canManage: boolean
  onToast: (text: string, kind?: 'info' | 'success' | 'error') => void
  initialSlug?: string | null
}) {
  const [pages, setPages] = useState<WikiPage[]>([])
  const [treeLoading, setTreeLoading] = useState(true)
  const [treeError, setTreeError] = useState<unknown>(null)

  const [slug, setSlug] = useState<string | null>(initialSlug ?? null)
  const [detail, setDetail] = useState<WikiPageDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<unknown>(null)

  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [draftSummary, setDraftSummary] = useState('')
  const [saving, setSaving] = useState(false)

  const [versionsOpen, setVersionsOpen] = useState(false)
  const [versions, setVersions] = useState<WikiPageVersion[]>([])
  const [versionsLoading, setVersionsLoading] = useState(false)
  const [versionsError, setVersionsError] = useState<unknown>(null)
  const [viewVersion, setViewVersion] = useState<WikiPageVersion | null>(null)

  const [unlockOpen, setUnlockOpen] = useState(false)
  const [unlockBusy, setUnlockBusy] = useState(false)

  const loadTree = useCallback(async () => {
    setTreeLoading(true)
    setTreeError(null)
    try {
      const data = await wikiApi.tree(kb.id)
      const list = [...(data.pages ?? [])].sort((a, b) => a.order_index - b.order_index)
      setPages(list)
      setSlug((prev) => {
        if (prev && list.some((p) => p.slug === prev)) return prev
        return list.length > 0 ? list[0].slug : null
      })
    } catch (err) {
      setTreeError(err)
      setPages([])
    } finally {
      setTreeLoading(false)
    }
  }, [kb.id])

  const loadPage = useCallback(
    async (targetSlug: string) => {
      setDetailLoading(true)
      setDetailError(null)
      try {
        const data = await wikiApi.page(kb.id, targetSlug)
        setDetail(data)
        setDraft(data.version?.content_md ?? '')
        setDraftSummary(data.page?.summary ?? '')
      } catch (err) {
        setDetailError(err)
        setDetail(null)
      } finally {
        setDetailLoading(false)
      }
    },
    [kb.id],
  )

  useEffect(() => {
    void loadTree()
  }, [loadTree])

  useEffect(() => {
    if (slug) void loadPage(slug)
  }, [slug, loadPage])

  useEffect(() => {
    if (initialSlug) setSlug(initialSlug)
  }, [initialSlug])

  useEffect(() => {
    setEditing(false)
    setViewVersion(null)
  }, [slug])

  const openVersions = async () => {
    if (!slug) return
    setVersionsOpen(true)
    setVersionsLoading(true)
    setVersionsError(null)
    try {
      const data = await wikiApi.versions(kb.id, slug)
      const list = Array.isArray(data) ? data : data.items ?? []
      setVersions([...list].sort((a, b) => b.version_no - a.version_no))
    } catch (err) {
      setVersionsError(err)
      setVersions([])
    } finally {
      setVersionsLoading(false)
    }
  }

  const save = async () => {
    if (!slug || !detail) return
    if (draft.trim().length < 10) {
      onToast('正文至少 10 个字符', 'error')
      return
    }
    setSaving(true)
    try {
      await wikiApi.save(kb.id, slug, draft, draftSummary || undefined)
      setEditing(false)
      onToast('已保存人工版本，该页索引正在重建', 'success')
      await loadPage(slug)
      await loadTree()
    } catch (err) {
      onToast(errorText(err), 'error')
    } finally {
      setSaving(false)
    }
  }

  const unlock = async () => {
    if (!slug) return
    setUnlockBusy(true)
    try {
      await wikiApi.unlock(kb.id, slug)
      onToast('已解除人工锁定，下次自动生成可覆盖该页', 'success')
      setUnlockOpen(false)
      await loadPage(slug)
      await loadTree()
    } catch (err) {
      onToast(errorText(err), 'error')
    } finally {
      setUnlockBusy(false)
    }
  }

  const publish = async () => {
    if (!slug) return
    try {
      await wikiApi.publish(kb.id, slug)
      onToast('页面已发布', 'success')
      await loadPage(slug)
      await loadTree()
    } catch (err) {
      onToast(errorText(err), 'error')
    }
  }

  const grouped = useMemo(() => groupPages(pages), [pages])

  if (treeLoading) {
    return (
      <div className="card card-pad">
        <SkeletonLines rows={6} />
      </div>
    )
  }

  if (treeError) {
    return (
      <div className="card">
        <ErrorState error={treeError} onRetry={() => void loadTree()} />
      </div>
    )
  }

  if (pages.length === 0) {
    return (
      <div className="card">
        <EmptyState
          title="该知识库还没有 Wiki 页面"
          hint={
            kb.mode === 'wiki'
              ? '文档入库完成后，在「设置」里点击「重建 Wiki」即可自动归纳生成页面。'
              : '当前知识库为基础分块模式，切换到 Wiki 增强并重建后才会生成页面。'
          }
        />
      </div>
    )
  }

  const page = detail?.page ?? pages.find((p) => p.slug === slug) ?? null

  return (
    <div className="wiki-layout">
      <div className="wiki-tree">
        {grouped.map((group) => (
          <div key={group.key}>
            {group.label ? (
              <div className="tiny muted" style={{ padding: '8px 9px 4px', fontWeight: 600 }}>
                {group.label}
              </div>
            ) : null}
            {group.items.map((p) => (
              <div
                key={p.slug}
                className={`wiki-tree-item ${p.slug === slug ? 'active' : ''}`}
                onClick={() => setSlug(p.slug)}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') setSlug(p.slug)
                }}
                title={p.summary || p.title}
              >
                <span className={`wiki-mark ${markClass(p)}`}>
                  {p.status === 'published' ? '●' : p.status === 'needs_review' ? '◐' : '○'}
                </span>
                <span className="wiki-tree-title">{p.title}</span>
                {p.human_locked ? <span title="人工锁定">🔒</span> : null}
              </div>
            ))}
          </div>
        ))}
      </div>

      <div className="wiki-main">
        {detailLoading ? (
          <div className="card card-pad">
            <SkeletonLines rows={8} />
          </div>
        ) : detailError ? (
          <div className="card">
            <ErrorState error={detailError} onRetry={() => slug && void loadPage(slug)} />
          </div>
        ) : !detail || !page ? (
          <div className="card">
            <EmptyState title="请选择左侧页面" hint="从目录中选择一个 Wiki 页面查看正文与来源依据。" />
          </div>
        ) : (
          <>
            <div className="wiki-doc">
              <div className="wiki-doc-head">
                <h2>{page.title}</h2>
                <Badge tone={taskStatusClass(page.status)}>
                  {wikiStatusLabel(page.status, page.status_label)}
                </Badge>
                {page.human_locked ? <Badge tone="info">人工锁定</Badge> : null}
                <div className="spacer" />
                {canManage ? (
                  <>
                    {editing ? (
                      <>
                        <Button size="sm" variant="ghost" onClick={() => setEditing(false)} disabled={saving}>
                          取消
                        </Button>
                        <Button size="sm" variant="primary" loading={saving} onClick={() => void save()}>
                          保存
                        </Button>
                      </>
                    ) : (
                      <>
                        {page.status === 'draft' ? (
                          <Button size="sm" onClick={() => void publish()}>
                            发布
                          </Button>
                        ) : null}
                        <Button size="sm" variant="primary" onClick={() => setEditing(true)}>
                          编辑校对
                        </Button>
                      </>
                    )}
                  </>
                ) : null}
                <Button size="sm" variant="ghost" onClick={() => void openVersions()}>
                  版本历史
                </Button>
              </div>

              <div className="tiny muted" style={{ marginBottom: 12 }}>
                共 {page.section_count} 个章节 · 更新于 {formatTime(page.updated_at)}
                {detail.version ? ` · 当前版本 v${detail.version.version_no}（${originLabel(detail.version.origin)}）` : ''}
              </div>

              {page.human_locked && canManage && !editing ? (
                <div className="insufficient-banner" style={{ marginBottom: 14 }}>
                  <span>🔒</span>
                  <div style={{ flex: 1 }}>
                    该页已被人工锁定，自动生成不会覆盖当前内容。
                  </div>
                  <button type="button" className="btn-link" onClick={() => setUnlockOpen(true)}>
                    解除锁定
                  </button>
                </div>
              ) : null}

              {detail.page?.summary ? <div className="wiki-summary">{detail.page.summary}</div> : null}

              {editing ? (
                <div>
                  <div className="field">
                    <span className="field-label">摘要</span>
                    <TextArea
                      value={draftSummary}
                      onChange={(e) => setDraftSummary(e.target.value)}
                      rows={2}
                      placeholder="页面摘要（可选）"
                    />
                  </div>
                  <div className="field">
                    <span className="field-label">正文（Markdown）</span>
                    <TextArea
                      className="code"
                      value={draft}
                      onChange={(e) => setDraft(e.target.value)}
                      spellCheck={false}
                    />
                  </div>
                  <div className="tiny muted">
                    保存后会生成人工版本并标记 human_locked，同时重建该页索引。
                  </div>
                </div>
              ) : (
                <Markdown content={detail.version?.content_md || ''} />
              )}
            </div>

            <div className="panel">
              <div className="panel-head">
                来源依据
                <span className="tiny muted">{detail.evidence?.length ?? 0} 条</span>
              </div>
              <div className="panel-body">
                {detail.evidence && detail.evidence.length > 0 ? (
                  detail.evidence.map((ev, i) => (
                    <div className="evidence-ref" key={`${ev.doc_name}-${i}`}>
                      <div className="evidence-ref-head">
                        <strong>{ev.doc_name || '未知文档'}</strong>
                        {ev.version_no ? <span>v{ev.version_no}</span> : null}
                        {ev.locator ? <span>· {ev.locator}</span> : null}
                      </div>
                      <div className="evidence-quote">{ev.quote}</div>
                    </div>
                  ))
                ) : (
                  <div className="muted small">该页面暂无来源依据记录。</div>
                )}
              </div>
            </div>

            <div className="panel">
              <div className="panel-head">关联页面</div>
              <div className="panel-body">
                {detail.related && detail.related.length > 0 ? (
                  <div className="related-chips">
                    {detail.related.map((r) => (
                      <span key={r.slug} className="chip" onClick={() => setSlug(r.slug)}>
                        {r.title}
                      </span>
                    ))}
                  </div>
                ) : (
                  <div className="muted small">暂无关联页面。</div>
                )}
              </div>
            </div>
          </>
        )}
      </div>

      <Modal
        open={versionsOpen}
        title={`版本历史 · ${page?.title ?? ''}`}
        onClose={() => {
          setVersionsOpen(false)
          setViewVersion(null)
        }}
        width={720}
        footer={
          <Button variant="ghost" onClick={() => setVersionsOpen(false)}>
            关闭
          </Button>
        }
      >
        {versionsLoading ? (
          <div className="state-block inline">
            <Spinner size={14} /> 版本加载中…
          </div>
        ) : versionsError ? (
          <ErrorState error={versionsError} />
        ) : versions.length === 0 ? (
          <EmptyState title="暂无版本记录" />
        ) : viewVersion ? (
          <>
            <div className="row" style={{ marginBottom: 10 }}>
              <Button size="sm" variant="ghost" onClick={() => setViewVersion(null)}>
                ← 返回列表
              </Button>
              <span className="small muted">
                v{viewVersion.version_no} · {originLabel(viewVersion.origin)}
                {viewVersion.note ? ` · ${viewVersion.note}` : ''}
              </span>
            </div>
            {viewVersion.content_md ? (
              <Markdown content={viewVersion.content_md} />
            ) : (
              <EmptyState
                title="该版本未返回正文"
                hint={
                  viewVersion.is_current
                    ? '当前版本正文请关闭弹窗后查看。'
                    : '版本列表接口只返回元信息，历史正文需后端补充 content_md 字段后才能展示。'
                }
              />
            )}
          </>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>版本</th>
                  <th>来源</th>
                  <th>时间</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {versions.map((v) => (
                  <tr key={v.id}>
                    <td>v{v.version_no}</td>
                    <td>{originLabel(v.origin)}</td>
                    <td className="small muted">{formatTime(v.created_at)}</td>
                    <td className="actions">
                      <button type="button" className="btn-link" onClick={() => setViewVersion(v)}>
                        查看内容
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Modal>

      <Modal
        open={unlockOpen}
        title="解除人工锁定"
        onClose={() => setUnlockOpen(false)}
        width={440}
        footer={
          <>
            <Button variant="ghost" onClick={() => setUnlockOpen(false)} disabled={unlockBusy}>
              取消
            </Button>
            <Button variant="primary" loading={unlockBusy} onClick={() => void unlock()}>
              确认解除
            </Button>
          </>
        }
      >
        <div className="confirm-text">
          解除后，下次自动生成 Wiki 时可以覆盖该页内容，你手工修改的内容可能被替换。建议先复制备份。
        </div>
      </Modal>
    </div>
  )
}

function groupPages(pages: WikiPage[]): Array<{ key: string; label: string; items: WikiPage[] }> {
  const hasParents = pages.some((p) => p.parent_slug)
  if (!hasParents) return [{ key: 'all', label: '', items: pages }]
  const roots = pages.filter((p) => !p.parent_slug)
  const groups: Array<{ key: string; label: string; items: WikiPage[] }> = []
  roots.forEach((root) => {
    groups.push({ key: root.slug, label: root.title, items: [root] })
    const children = pages.filter((p) => p.parent_slug === root.slug)
    if (children.length) {
      groups.push({ key: `${root.slug}-children`, label: '', items: children })
    }
  })
  const assigned = new Set(groups.flatMap((g) => g.items.map((p) => p.slug)))
  const orphans = pages.filter((p) => !assigned.has(p.slug))
  if (orphans.length) groups.push({ key: 'orphans', label: '其他', items: orphans })
  return groups
}

function markClass(p: WikiPage): string {
  if (p.status === 'published') return 'test-ok'
  if (p.status === 'needs_review') return 'test-fail'
  return 'muted'
}

function originLabel(origin: string | undefined): string {
  switch (origin) {
    case 'auto':
      return '自动生成'
    case 'human':
      return '人工编辑'
    case 'import':
      return '导入'
    default:
      return origin || '未知来源'
  }
}

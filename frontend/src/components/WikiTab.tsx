import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ApiError, wikiApi } from '../api'
import type {
  KnowledgeBase,
  WikiPage,
  WikiPageDetail,
  WikiPageVersion,
} from '../types'
import { errorText, formatTime, taskStatusClass, wikiStatusLabel } from '../utils'
import { IconAlert, IconArrowLeft, IconLock } from './icons'
import {
  Badge,
  Button,
  ConfirmDialog,
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
  isAdmin,
  canManage,
  onToast,
  initialSlug,
}: {
  kb: KnowledgeBase
  isAdmin: boolean
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
  const [unavailable, setUnavailable] = useState(false)
  const unavailableRef = useRef(false)
  const revocationRef = useRef(0)
  const detailRef = useRef<WikiPageDetail | null>(null)
  const selectedSlugRef = useRef<string | null>(initialSlug ?? null)
  const selectionRef = useRef(0)
  const currentKbIdRef = useRef(kb.id)
  currentKbIdRef.current = kb.id
  const pageRequestRef = useRef(0)
  const treeRequestRef = useRef(0)
  const foregroundTreeRequestRef = useRef<number | null>(null)
  const versionsRequestRef = useRef(0)
  const mountedRef = useRef(true)
  const treeButtonRefs = useRef(new Map<string, HTMLButtonElement>())
  const focusSelectionRef = useRef(false)

  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [draftSummary, setDraftSummary] = useState('')
  const [saving, setSaving] = useState(false)
  const savingRef = useRef(false)
  const [pendingSlug, setPendingSlug] = useState<string | null>(null)
  const navigationTriggerRef = useRef<HTMLElement | null>(null)
  const previousInitialSlugRef = useRef(initialSlug)
  const deferredInitialSlugRef = useRef<string | null>(null)
  const deferredTreeSelectionRef = useRef(false)

  const [versionsOpen, setVersionsOpen] = useState(false)
  const [versions, setVersions] = useState<WikiPageVersion[]>([])
  const [versionsLoading, setVersionsLoading] = useState(false)
  const [versionsError, setVersionsError] = useState<unknown>(null)
  const [viewVersion, setViewVersion] = useState<WikiPageVersion | null>(null)

  const [unlockOpen, setUnlockOpen] = useState(false)
  const [unlockBusy, setUnlockBusy] = useState(false)

  const markUnavailable = useCallback((targetSlug: string) => {
    if (!mountedRef.current || selectedSlugRef.current !== targetSlug) return
    unavailableRef.current = true
    revocationRef.current += 1
    pageRequestRef.current += 1
    treeRequestRef.current += 1
    versionsRequestRef.current += 1
    setUnavailable(true)
    setPages((previous) => previous.filter((page) => page.slug !== targetSlug))
    setTreeLoading(false)
    setTreeError(null)
    setDetailLoading(false)
    setDetailError(null)
    setVersionsOpen(false)
    setViewVersion(null)
    setVersions([])
    setUnlockOpen(false)
  }, [])

  // All page selection goes through here so an earlier response becomes stale immediately.
  const selectSlug = useCallback((next: string | null, focusSelection = false) => {
    if (selectedSlugRef.current === next) return
    if (savingRef.current) {
      deferredTreeSelectionRef.current = true
      return
    }
    selectedSlugRef.current = next
    selectionRef.current += 1
    pageRequestRef.current += 1
    versionsRequestRef.current += 1
    unavailableRef.current = false
    detailRef.current = null
    focusSelectionRef.current = focusSelection
    setUnavailable(false)
    setVersionsOpen(false)
    setSlug(next)
    setDetail(null)
    setDetailError(null)
    setDetailLoading(Boolean(next))
  }, [])

  const loadTree = useCallback(async (background = false) => {
    const requestId = ++treeRequestRef.current
    const selectionId = selectionRef.current
    foregroundTreeRequestRef.current = requestId
    const isCurrent = () =>
      mountedRef.current && requestId === treeRequestRef.current && currentKbIdRef.current === kb.id
    if (!background) {
      setTreeLoading(true)
      setTreeError(null)
    }
    try {
      const data = await wikiApi.tree(kb.id)
      if (!isCurrent() || selectionId !== selectionRef.current) return
      const list = [...(data.pages ?? [])].sort((a, b) => a.order_index - b.order_index)
      const current = selectedSlugRef.current
      setPages(unavailableRef.current ? list.filter((page) => page.slug !== current) : list)
      if (current && !list.some((p) => p.slug === current) && !isAdmin &&
          (detailRef.current || unavailableRef.current)) {
        markUnavailable(current)
      } else if ((!current || !list.some((p) => p.slug === current)) && (!background || !current)) {
        selectSlug(list[0]?.slug ?? null)
      }
    } catch (err) {
      if (!isCurrent() || selectionId !== selectionRef.current) return
      const current = selectedSlugRef.current
      if (!isAdmin && current && isVisibilityError(err)) {
        setPages([])
        markUnavailable(current)
      } else if (!background) {
        setTreeError(err)
        setPages([])
      }
    } finally {
      if (foregroundTreeRequestRef.current === requestId) foregroundTreeRequestRef.current = null
      if (isCurrent()) setTreeLoading(false)
    }
  }, [kb.id, isAdmin, markUnavailable, selectSlug])

  const loadPage = useCallback(
    async (targetSlug: string) => {
      if (!mountedRef.current || currentKbIdRef.current !== kb.id || selectedSlugRef.current !== targetSlug) return
      const requestId = ++pageRequestRef.current
      const isCurrent = () =>
        mountedRef.current &&
        currentKbIdRef.current === kb.id &&
        selectedSlugRef.current === targetSlug &&
        requestId === pageRequestRef.current
      setDetailLoading(true)
      setDetailError(null)
      try {
        const data = await wikiApi.page(kb.id, targetSlug)
        if (!isCurrent() || unavailableRef.current) return
        detailRef.current = data
        setDetail(data)
        setDraft(data.version?.content_md ?? '')
        setDraftSummary(data.page?.summary ?? '')
      } catch (err) {
        if (!isCurrent()) return
        if (!isAdmin && isVisibilityError(err)) {
          markUnavailable(targetSlug)
        } else if (!unavailableRef.current) {
          detailRef.current = null
          setDetailError(err)
          setDetail(null)
        }
      } finally {
        if (isCurrent()) setDetailLoading(false)
      }
    },
    [kb.id, isAdmin, markUnavailable],
  )

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      pageRequestRef.current += 1
      treeRequestRef.current += 1
    }
  }, [])

  useEffect(() => {
    void loadTree()
  }, [loadTree])

  const treeStateRef = useRef({
    kbId: kb.id, pending: kb.index?.pending_tasks ?? 0, readyAt: kb.wiki_ready_at,
    count: kb.stats?.wiki_page_count ?? 0, refresh: false, refreshDetail: false,
  })
  useEffect(() => {
    const pending = kb.index?.pending_tasks ?? 0
    const count = kb.stats?.wiki_page_count ?? 0
    const readyAt = kb.wiki_ready_at
    const previous = treeStateRef.current
    if (previous.kbId !== kb.id) {
      treeStateRef.current = { kbId: kb.id, pending, count, readyAt, refresh: false, refreshDetail: false }
      return
    }
    if ((previous.pending > 0 && pending === 0) || previous.count !== count ||
        (Boolean(readyAt) && previous.readyAt !== readyAt)) {
      previous.refresh = true
    }
    previous.pending = pending
    previous.count = count
    previous.readyAt = readyAt
    // 编辑时仍更新目录，但保留选中页面与正文；详情等校对结束再刷新。
    if (previous.refresh) {
      previous.refresh = false
      previous.refreshDetail = true
      void loadTree(editing || saving)
    }
    if (previous.refreshDetail && !editing && !saving) {
      previous.refreshDetail = false
      if (selectedSlugRef.current) void loadPage(selectedSlugRef.current)
    }
  }, [kb.id, kb.index?.pending_tasks, kb.stats?.wiki_page_count, kb.wiki_ready_at, editing, saving, loadTree, loadPage])

  useEffect(() => {
    if (slug) void loadPage(slug)
    return () => { pageRequestRef.current += 1 }
  }, [slug, loadPage])

  // 已打开的页面也要复核可见性；成功复核不会覆盖正文或编辑中的草稿。
  useEffect(() => {
    if (isAdmin) return
    let alive = true
    let treeBusy = false
    let pageBusy = false
    let verifiedTree: { revocationId: number; pages: WikiPage[] } | null = null
    let verifiedDetail: { revocationId: number; requestId: number; detail: WikiPageDetail } | null = null
    const tick = () => {
      const targetSlug = selectedSlugRef.current
      const selectionId = selectionRef.current
      const pageRequestId = pageRequestRef.current
      const revocationId = revocationRef.current
      let treeRequestId = 0
      const isCurrent = () => alive && mountedRef.current && currentKbIdRef.current === kb.id &&
        selectionRef.current === selectionId && selectedSlugRef.current === targetSlug
      const restoreIfVisible = () => {
        if (!isCurrent() || !targetSlug || !unavailableRef.current || !verifiedTree || !verifiedDetail ||
            verifiedTree.revocationId !== revocationRef.current || verifiedDetail.revocationId !== revocationRef.current ||
            verifiedDetail.requestId !== pageRequestRef.current) return
        // 恢复时使用新详情，但草稿一直保留，只有用户确认放弃才能切换。
        detailRef.current = verifiedDetail.detail
        setDetail(verifiedDetail.detail)
        setPages(verifiedTree.pages)
        unavailableRef.current = false
        setUnavailable(false)
        setDetailError(null)
        setDetailLoading(false)
      }

      if (!treeBusy && foregroundTreeRequestRef.current !== treeRequestRef.current) {
        treeBusy = true
        treeRequestId = ++treeRequestRef.current
        void wikiApi.tree(kb.id, { silent403: true }).then((data) => {
          if (!isCurrent() || treeRequestId !== treeRequestRef.current ||
              revocationId !== revocationRef.current) return
          const list = [...(data.pages ?? [])].sort((a, b) => a.order_index - b.order_index)
          setPages(unavailableRef.current ? list.filter((page) => page.slug !== targetSlug) : list)
          setTreeLoading(false)
          setTreeError(null)
          if (!targetSlug) {
            if (list.length) selectSlug(list[0].slug)
            return
          }
          if (!list.some((page) => page.slug === targetSlug)) {
            markUnavailable(targetSlug)
            return
          }
          if (revocationId === revocationRef.current) {
            verifiedTree = { revocationId, pages: list }
            restoreIfVisible()
          }
        }).catch((err: unknown) => {
          if (!isCurrent() || treeRequestId !== treeRequestRef.current || !isVisibilityError(err)) return
          setPages([])
          if (targetSlug) markUnavailable(targetSlug)
          else {
            setTreeLoading(false)
            setTreeError(null)
          }
        }).finally(() => { treeBusy = false })
      }

      if (targetSlug && !pageBusy) {
        pageBusy = true
        void wikiApi.page(kb.id, targetSlug, { silent403: true }).then((data) => {
          if (!isCurrent() || pageRequestId !== pageRequestRef.current ||
              revocationId !== revocationRef.current) return
          verifiedDetail = { revocationId, requestId: pageRequestId, detail: data }
          restoreIfVisible()
        }).catch((err: unknown) => {
          if (isCurrent() && pageRequestId === pageRequestRef.current && isVisibilityError(err)) {
            markUnavailable(targetSlug)
          }
        }).finally(() => { pageBusy = false })
      }
    }
    const timer = window.setInterval(tick, 5000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [isAdmin, kb.id, slug, markUnavailable, selectSlug])

  useEffect(() => {
    setEditing(false)
    setViewVersion(null)
  }, [slug])

  const dirty = editing && detail !== null && (
    draft !== (detail.version?.content_md ?? '') || draftSummary !== (detail.page?.summary ?? '')
  )

  const requestNavigation = useCallback((next: string, trigger?: HTMLElement | null, focusSelection = false) => {
    if (savingRef.current || next === selectedSlugRef.current) return
    if (dirty) {
      navigationTriggerRef.current = trigger ?? null
      setPendingSlug(next)
      return
    }
    setEditing(false)
    selectSlug(next, focusSelection)
  }, [dirty, selectSlug])

  // A changed deep link is handled through the same draft guard, after any save finishes.
  useEffect(() => {
    if (previousInitialSlugRef.current !== initialSlug) {
      previousInitialSlugRef.current = initialSlug
      deferredInitialSlugRef.current = initialSlug ?? null
    }
    if (saving || !deferredInitialSlugRef.current) return
    const next = deferredInitialSlugRef.current
    deferredInitialSlugRef.current = null
    requestNavigation(next)
  }, [initialSlug, saving, requestNavigation])

  useEffect(() => {
    if (saving || treeLoading || treeError || !deferredTreeSelectionRef.current) return
    deferredTreeSelectionRef.current = false
    if (unavailableRef.current) return
    const current = selectedSlugRef.current
    if (!current || !pages.some((p) => p.slug === current)) selectSlug(pages[0]?.slug ?? null)
  }, [saving, treeLoading, treeError, pages, selectSlug])

  useEffect(() => {
    if (!slug || treeLoading || !focusSelectionRef.current) return
    focusSelectionRef.current = false
    treeButtonRefs.current.get(slug)?.focus()
  }, [slug, treeLoading])

  const cancelNavigation = () => {
    const trigger = navigationTriggerRef.current
    navigationTriggerRef.current = null
    setPendingSlug(null)
    window.requestAnimationFrame(() => {
      if (trigger?.isConnected) trigger.focus()
    })
  }

  const confirmNavigation = () => {
    if (!pendingSlug || savingRef.current) return
    const next = pendingSlug
    navigationTriggerRef.current = null
    setPendingSlug(null)
    setEditing(false)
    selectSlug(next, true)
  }

  const openVersions = async () => {
    if (!slug || unavailableRef.current) return
    const requestId = ++versionsRequestRef.current
    const isCurrent = () => mountedRef.current && currentKbIdRef.current === kb.id &&
      selectedSlugRef.current === slug && requestId === versionsRequestRef.current && !unavailableRef.current
    setVersionsOpen(true)
    setVersionsLoading(true)
    setVersionsError(null)
    try {
      const data = await wikiApi.versions(kb.id, slug)
      if (!isCurrent()) return
      const list = Array.isArray(data) ? data : data.items ?? []
      setVersions([...list].sort((a, b) => b.version_no - a.version_no))
    } catch (err) {
      if (!isCurrent()) return
      if (!isAdmin && isVisibilityError(err)) markUnavailable(slug)
      else {
        setVersionsError(err)
        setVersions([])
      }
    } finally {
      if (isCurrent()) setVersionsLoading(false)
    }
  }

  const save = async () => {
    if (!slug || !detail || savingRef.current || unavailableRef.current) return
    const revocationId = revocationRef.current
    if (draft.trim().length < 10) {
      onToast('正文至少 10 个字符', 'error')
      return
    }
    savingRef.current = true
    setSaving(true)
    try {
      await wikiApi.save(kb.id, slug, draft, draftSummary || undefined)
      if (!mountedRef.current || currentKbIdRef.current !== kb.id || selectedSlugRef.current !== slug ||
          unavailableRef.current || revocationRef.current !== revocationId) return
      setEditing(false)
      onToast('已保存人工版本，该页索引正在重建', 'success')
      await loadPage(slug)
      await loadTree()
    } catch (err) {
      if (!mountedRef.current || currentKbIdRef.current !== kb.id || selectedSlugRef.current !== slug) return
      if (!isAdmin && isVisibilityError(err)) markUnavailable(slug)
      else onToast(errorText(err), 'error')
    } finally {
      savingRef.current = false
      if (mountedRef.current) setSaving(false)
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

  const grouped = useMemo(
    () => groupPages(unavailable ? pages.filter((page) => page.slug !== slug) : pages),
    [pages, unavailable, slug],
  )
  const visibleRelated = (detail?.related ?? []).filter(
    (related) => isAdmin || pages.some((item) => item.slug === related.slug),
  )

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

  if (pages.length === 0 && !unavailable && !(detail && (editing || saving))) {
    return (
      <div className="card">
        <EmptyState
          title="暂无可查看的 Wiki 页面"
          hint={
            kb.mode === 'wiki'
              ? '文档入库会触发 Wiki 生成；若页面暂不可用，请查看任务或联系管理员。'
              : '当前为基础分块模式；切换到 Wiki 增强后可查看可用页面。'
          }
        />
      </div>
    )
  }

  const page = unavailable ? null : detail?.page ?? pages.find((p) => p.slug === slug) ?? null

  return (
    <div className="wiki-layout">
      <nav className="wiki-tree" aria-label="Wiki 目录">
        {grouped.map((group) => (
          <div key={group.key}>
            {group.label ? (
              <div className="tiny muted" style={{ padding: '8px 9px 4px', fontWeight: 600 }}>
                {group.label}
              </div>
            ) : null}
            {group.items.map((p) => (
              <button
                key={p.slug}
                ref={(node) => {
                  if (node) treeButtonRefs.current.set(p.slug, node)
                  else treeButtonRefs.current.delete(p.slug)
                }}
                type="button"
                className={`wiki-tree-item ${p.slug === slug ? 'active' : ''}`}
                aria-current={p.slug === slug ? 'page' : undefined}
                aria-label={`${p.title}，${wikiStatusLabel(p.status, p.status_label)}${p.human_locked ? '，人工锁定' : ''}`}
                disabled={saving}
                onClick={(e) => requestNavigation(p.slug, e.currentTarget)}
                title={p.summary ? `${p.title} · ${p.summary}` : p.title}
              >
                <span className={`wiki-mark ${markClass(p)}`} aria-hidden="true" />
                <span className="wiki-tree-title">{p.title}</span>
                {p.human_locked ? (
                  <span title="人工锁定" aria-hidden="true" className="wiki-lock">
                    <IconLock size={12} />
                  </span>
                ) : null}
              </button>
            ))}
          </div>
        ))}
      </nav>

      <div className="wiki-main">
        {unavailable ? (
          <div className="card">
            <EmptyState
              title="该页面暂不可查看"
              hint={editing
                ? '页面权限或状态已变化；未保存的草稿暂存于当前页面，恢复后可继续编辑。'
                : '页面权限或状态已变化，请稍后重试或联系管理员。'}
            />
          </div>
        ) : detailLoading ? (
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
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => {
                            setDraft(detail.version?.content_md ?? '')
                            setDraftSummary(detail.page?.summary ?? '')
                            setEditing(false)
                          }}
                          disabled={saving}
                        >
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
                        <Button size="sm" variant="primary" onClick={() => {
                          setDraft(detail.version?.content_md ?? '')
                          setDraftSummary(detail.page?.summary ?? '')
                          setEditing(true)
                        }}>
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

              {isAdmin && (detail.deleted_generation_sources?.length ?? 0) > 0 ? (
                <div className="insufficient-banner" style={{ marginBottom: 14 }}>
                  <span className="banner-icon"><IconAlert size={16} /></span>
                  <div style={{ flex: 1, minWidth: 0, overflowWrap: 'anywhere' }}>
                    <strong>页面待复核</strong>
                    <div style={{ marginTop: 2 }}>
                      生成时读取或引用的已删除文档：{detail.deleted_generation_sources?.map((source) => source.name).join('、')}。
                      该页面暂时对普通用户隐藏；复核后可使用有效来源重新生成。
                    </div>
                  </div>
                </div>
              ) : null}

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
                {visibleRelated.length > 0 ? (
                  <div className="related-chips">
                    {visibleRelated.map((r) => (
                      <button
                        key={r.slug}
                        type="button"
                        className="chip"
                        disabled={saving}
                        onClick={(e) => requestNavigation(r.slug, e.currentTarget, true)}
                      >
                        {r.title}
                      </button>
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
        open={versionsOpen && !unavailable}
        title={`版本历史 · ${page?.title ?? ''}`}
        onClose={() => {
          versionsRequestRef.current += 1
          setVersionsOpen(false)
          setViewVersion(null)
        }}
        width={720}
        footer={
          <Button variant="ghost" onClick={() => {
            versionsRequestRef.current += 1
            setVersionsOpen(false)
            setViewVersion(null)
          }}>
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
                <IconArrowLeft size={14} /> 返回列表
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

      <ConfirmDialog
        open={Boolean(pendingSlug)}
        title="放弃未保存的修改？"
        message="切换页面会丢弃当前未保存的正文和摘要。"
        confirmText="放弃并切换"
        danger
        busy={saving}
        onConfirm={confirmNavigation}
        onCancel={cancelNavigation}
      />

      <Modal
        open={unlockOpen && !unavailable}
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

function isVisibilityError(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 403 || error.status === 404)
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
  if (p.status === 'published') return 'published'
  if (p.status === 'needs_review') return 'review'
  return ''
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

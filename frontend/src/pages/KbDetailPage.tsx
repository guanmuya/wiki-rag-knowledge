import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useAuth, useToast } from '../auth'
import { kbApi } from '../api'
import type { KnowledgeBase } from '../types'
import { formatTime, kbStatusLabel, modeLabel, taskStatusClass } from '../utils'
import { Badge, Button, ErrorState, SkeletonLines, Tabs } from '../components/ui'
import DocumentsTab from '../components/DocumentsTab'
import WikiTab from '../components/WikiTab'
import SettingsTab from '../components/SettingsTab'

type TabKey = 'documents' | 'wiki' | 'settings'

export default function KbDetailPage() {
  const { id } = useParams<{ id: string }>()
  const kbId = Number(id)
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const { isAdmin, canManage } = useAuth()
  const { push } = useToast()

  const [kb, setKb] = useState<KnowledgeBase | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<unknown>(null)

  const tabParam = searchParams.get('tab')
  const tab: TabKey =
    tabParam === 'wiki' || tabParam === 'settings' ? (tabParam as TabKey) : 'documents'
  const slugParam = searchParams.get('slug')

  const load = useCallback(async () => {
    if (!Number.isFinite(kbId)) {
      setError(new Error('知识库 ID 不合法'))
      setLoading(false)
      return
    }
    try {
      const data = await kbApi.get(kbId)
      setKb(data)
      setError(null)
    } catch (err) {
      setError(err)
    } finally {
      setLoading(false)
    }
  }, [kbId])

  useEffect(() => {
    setLoading(true)
    void load()
  }, [load])

  const setTab = (next: TabKey) => {
    const params = new URLSearchParams(searchParams)
    params.set('tab', next)
    if (next !== 'wiki') params.delete('slug')
    setSearchParams(params, { replace: true })
  }

  const openWikiPage = (slug: string) => {
    const params = new URLSearchParams(searchParams)
    params.set('tab', 'wiki')
    if (slug) params.set('slug', slug)
    else params.delete('slug')
    setSearchParams(params, { replace: true })
  }

  if (loading) {
    return (
      <div className="card card-pad">
        <SkeletonLines rows={5} />
      </div>
    )
  }

  if (error || !kb) {
    return (
      <div className="card">
        <ErrorState error={error ?? new Error('知识库不存在或无权访问')} onRetry={() => void load()} />
      </div>
    )
  }

  const manage = isAdmin || canManage(kb.id) || kb.my_level === 'manage'

  return (
    <>
      <section className="card detail-hero">
      <div className="detail-head">
        <button type="button" className="back-link" onClick={() => navigate('/kbs')}>
          ← 知识库
        </button>
        <div className="detail-title">
          <h1>{kb.name}</h1>
          <Badge tone={taskStatusClass(kb.status)}>
            {kbStatusLabel(kb.status, kb.status_label)}
          </Badge>
          <Badge tone={kb.mode === 'wiki' ? 'info' : 'muted'}>
            {modeLabel(kb.mode, kb.mode_label)}
          </Badge>
          <Badge tone={manage ? 'success' : 'muted'}>{manage ? '可管理' : '可读'}</Badge>
        </div>
        {kb.description ? (
          <div className="small muted" style={{ marginTop: 6 }}>
            {kb.description}
          </div>
        ) : null}
        <div className="detail-meta">
          <span>
            <span className="k">文档</span>
            {kb.stats?.doc_count ?? 0}
          </span>
          <span>
            <span className="k">分块</span>
            {kb.stats?.chunk_count ?? 0}
          </span>
          <span>
            <span className="k">Wiki 页面</span>
            {kb.stats?.wiki_page_count ?? 0}
          </span>
          <span>
            <span className="k">向量维度</span>
            {kb.embed_dim || '—'}
          </span>
          <span>
            <span className="k">创建</span>
            {formatTime(kb.created_at)}
          </span>
          {kb.embed_signature ? (
            <span>
              <span className="k">embed</span>
              <span className="sig-box">{kb.embed_signature}</span>
            </span>
          ) : null}
        </div>
      </div>

        <Tabs<TabKey>
          value={tab}
          onChange={setTab}
          items={[
            { key: 'documents', label: '文档', badge: kb.stats?.doc_count ?? 0 },
            { key: 'wiki', label: 'Wiki', badge: kb.stats?.wiki_page_count ?? 0 },
            { key: 'settings', label: '设置' },
          ]}
        />
      </section>

      <div className="detail-body">
        {tab === 'documents' ? (
          <DocumentsTab
            kb={kb}
            canManage={manage}
            onChanged={load}
            onToast={push}
          />
        ) : tab === 'wiki' ? (
          <WikiTab
            kb={kb}
            canManage={manage}
            onToast={push}
            initialSlug={slugParam}
          />
        ) : (
          <SettingsTab
            kb={kb}
            isAdmin={isAdmin}
            canManage={manage}
            onToast={push}
            onChanged={load}
          />
        )}
      </div>

      {!manage ? (
        <div className="tiny muted" style={{ marginTop: 10 }}>
          当前账号对该库只有只读权限，可查看文档、Wiki 与证据来源，上传与重建需「可管理」等级。
        </div>
      ) : null}

      <div style={{ marginTop: 12, display: 'flex', gap: 8 }}>
        <Button size="sm" variant="ghost" onClick={() => navigate('/chat')}>
          去问答 →
        </Button>
        {tab !== 'wiki' && (kb.stats?.wiki_page_count ?? 0) > 0 ? (
          <Button size="sm" variant="ghost" onClick={() => openWikiPage(slugParam ?? '')}>
            查看 Wiki
          </Button>
        ) : null}
      </div>
    </>
  )
}

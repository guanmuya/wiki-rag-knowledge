import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth, useToast } from '../auth'
import { kbApi } from '../api'
import type { KbMode, KnowledgeBase } from '../types'
import {
  errorText,
  formatNumber,
  formatTime,
  kbStatusLabel,
  modeLabel,
  taskStatusClass,
} from '../utils'
import {
  Badge,
  Button,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Field,
  Modal,
  Select,
  SkeletonLines,
  TextArea,
  TextInput,
} from '../components/ui'
import { IconChevronRight } from '../components/icons'

/** 卡片视图图标：2×2 网格，和「卡片」的语义一致。 */
function CardViewIcon() {
  return (
    <svg
      viewBox="0 0 16 16"
      width="14"
      height="14"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      aria-hidden="true"
    >
      <rect x="2.25" y="2.25" width="4.75" height="4.75" rx="1.25" />
      <rect x="9" y="2.25" width="4.75" height="4.75" rx="1.25" />
      <rect x="2.25" y="9" width="4.75" height="4.75" rx="1.25" />
      <rect x="9" y="9" width="4.75" height="4.75" rx="1.25" />
    </svg>
  )
}

/** 表格视图图标：带表头和首列分隔线的表格。 */
function TableViewIcon() {
  return (
    <svg
      viewBox="0 0 16 16"
      width="14"
      height="14"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      aria-hidden="true"
    >
      <rect x="2.25" y="2.75" width="11.5" height="10.5" rx="1.5" />
      <path d="M2.25 6.25h11.5M6.5 6.25v7" />
    </svg>
  )
}

export default function KbListPage() {
  const { isAdmin, refreshMe } = useAuth()
  const { push } = useToast()
  const navigate = useNavigate()
  const [kbs, setKbs] = useState<KnowledgeBase[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<unknown>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [mode, setMode] = useState<KbMode>('basic')
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState('')
  const [view, setView] = useState<'card' | 'table'>('card')
  const [deleteTarget, setDeleteTarget] = useState<KnowledgeBase | null>(null)
  const [deleteBusy, setDeleteBusy] = useState(false)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await kbApi.list()
      if (mounted.current) setKbs(data.items ?? [])
    } catch (err) {
      if (mounted.current) setError(err)
    } finally {
      if (mounted.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const openCreate = () => {
    setName('')
    setDescription('')
    setMode('basic')
    setFormError('')
    setCreateOpen(true)
  }

  const submitCreate = async () => {
    if (!name.trim()) {
      setFormError('请填写知识库名称')
      return
    }
    setSaving(true)
    setFormError('')
    try {
      const created = await kbApi.create({
        name: name.trim(),
        description: description.trim(),
        mode,
      })
      push(`知识库「${created.name}」已创建`, 'success')
      setCreateOpen(false)
      await load()
      await refreshMe()
    } catch (err) {
      setFormError(errorText(err))
    } finally {
      setSaving(false)
    }
  }

  const submitDelete = async () => {
    if (!deleteTarget) return
    setDeleteBusy(true)
    try {
      await kbApi.remove(deleteTarget.id)
      push(`知识库「${deleteTarget.name}」已删除`, 'success')
      setDeleteTarget(null)
      await load()
      await refreshMe()
    } catch (err) {
      push(errorText(err), 'error')
    } finally {
      setDeleteBusy(false)
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>知识库</h1>
          <div className="sub">
            {isAdmin
              ? '管理员可见全部知识库，可新建、配置与重建索引。'
              : '这里只列出已授权给你的知识库，权限等级决定你能否上传与校对。'}
          </div>
        </div>
        <div className="spacer" />
        <div className="page-head-actions">
          <div className="view-switch" role="group" aria-label="知识库视图切换">
            <button
              type="button"
              className={`view-switch-item ${view === 'card' ? 'active' : ''}`}
              aria-pressed={view === 'card'}
              onClick={() => setView('card')}
            >
              <CardViewIcon />
              卡片
            </button>
            <button
              type="button"
              className={`view-switch-item ${view === 'table' ? 'active' : ''}`}
              aria-pressed={view === 'table'}
              onClick={() => setView('table')}
            >
              <TableViewIcon />
              表格
            </button>
          </div>
          {isAdmin ? (
            <Button variant="primary" onClick={openCreate}>
              + 新建知识库
            </Button>
          ) : null}
        </div>
      </div>

      {loading ? (
        <div className="card card-pad">
          <SkeletonLines rows={5} />
        </div>
      ) : error ? (
        <div className="card">
          <ErrorState error={error} onRetry={() => void load()} />
        </div>
      ) : kbs.length === 0 ? (
        <div className="card">
          <EmptyState
            title="还没有可见的知识库"
            hint={
              isAdmin
                ? '点击右上角「新建知识库」开始，创建后即可上传文档。'
                : '请联系管理员为你授权知识库访问权限。'
            }
            action={
              isAdmin ? (
                <Button variant="primary" size="sm" onClick={openCreate}>
                  新建知识库
                </Button>
              ) : undefined
            }
          />
        </div>
      ) : view === 'card' ? (
        <div className="kb-grid">
          {kbs.map((kb) => (
            <article
              key={kb.id}
              className="kb-card"
              onClick={() => navigate(`/kbs/${kb.id}`)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === 'Enter') navigate(`/kbs/${kb.id}`)
              }}
            >
              <div className="kb-card-head">
                <div className="kb-card-title" title={kb.name}>
                  {kb.name}
                </div>
                <Badge tone={taskStatusClass(kb.status)}>
                  {kbStatusLabel(kb.status, kb.status_label)}
                </Badge>
              </div>
              <div className="kb-card-desc">{kb.description || '暂无描述'}</div>
              <div className="row row-wrap kb-card-meta">
                <span>{modeLabel(kb.mode, kb.mode_label)}</span>
                <span>{kb.my_level === 'manage' ? '可管理' : '可读'}</span>
                {kb.mode === 'wiki' ? (
                  <span className={kb.wiki_ready ? 'kb-meta-ok' : 'kb-meta-warn'}>
                    {kb.wiki_ready ? 'Wiki 已就绪' : 'Wiki 未生成'}
                  </span>
                ) : null}
              </div>
              <div className="kb-stats">
                <div className="stat-cell">
                  <span className="stat-value">{formatNumber(kb.stats?.doc_count)}</span>
                  <span className="stat-label">文档</span>
                </div>
                <div className="stat-cell">
                  <span className="stat-value">{formatNumber(kb.stats?.chunk_count)}</span>
                  <span className="stat-label">分块</span>
                </div>
                <div className="stat-cell">
                  <span className="stat-value">{formatNumber(kb.stats?.wiki_page_count)}</span>
                  <span className="stat-label">Wiki 页</span>
                </div>
                <div className="stat-cell">
                  <span className="stat-value">{kb.embed_dim || '—'}</span>
                  <span className="stat-label">向量维度</span>
                </div>
              </div>
              {kb.status_detail ? <div className="tiny muted">{kb.status_detail}</div> : null}
              <div className="kb-card-actions">
                <span className="tiny muted">进入详情 <IconChevronRight size={12} /></span>
                {isAdmin ? (
                  <button
                    type="button"
                    className="btn-link danger"
                    onClick={(e) => {
                      e.stopPropagation()
                      setDeleteTarget(kb)
                    }}
                  >
                    删除
                  </button>
                ) : null}
              </div>
            </article>
          ))}
        </div>
      ) : (
        <div className="card">
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>名称</th>
                  <th>模式</th>
                  <th>状态</th>
                  <th>文档</th>
                  <th>分块</th>
                  <th>Wiki 页</th>
                  <th>我的权限</th>
                  <th>创建时间</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {kbs.map((kb) => (
                  <tr key={kb.id}>
                    <td>
                      <div className="strong">{kb.name}</div>
                      <div className="tiny muted truncate" style={{ maxWidth: 320 }}>
                        {kb.description || '暂无描述'}
                      </div>
                    </td>
                    <td>
                      <Badge tone={kb.mode === 'wiki' ? 'info' : 'muted'}>
                        {modeLabel(kb.mode, kb.mode_label)}
                      </Badge>
                    </td>
                    <td>
                      <Badge tone={taskStatusClass(kb.status)}>
                        {kbStatusLabel(kb.status, kb.status_label)}
                      </Badge>
                    </td>
                    <td>{formatNumber(kb.stats?.doc_count)}</td>
                    <td>{formatNumber(kb.stats?.chunk_count)}</td>
                    <td>{formatNumber(kb.stats?.wiki_page_count)}</td>
                    <td>
                      <Badge tone={kb.my_level === 'manage' ? 'success' : 'muted'}>
                        {kb.my_level === 'manage' ? '可管理' : '可读'}
                      </Badge>
                    </td>
                    <td className="small muted nowrap">{formatTime(kb.created_at)}</td>
                    <td className="actions">
                      <div className="table-actions">
                        <button
                          type="button"
                          className="btn-link"
                          onClick={() => navigate(`/kbs/${kb.id}`)}
                        >
                          进入
                        </button>
                        {isAdmin ? (
                          <button
                            type="button"
                            className="btn-link danger"
                            onClick={() => setDeleteTarget(kb)}
                          >
                            删除
                          </button>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <Modal
        open={createOpen}
        title="新建知识库"
        onClose={() => setCreateOpen(false)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setCreateOpen(false)} disabled={saving}>
              取消
            </Button>
            <Button variant="primary" loading={saving} onClick={() => void submitCreate()}>
              创建
            </Button>
          </>
        }
      >
        {formError ? <div className="login-error">{formError}</div> : null}
        <Field label="名称" required>
          <TextInput
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="例如：员工手册"
            maxLength={128}
            autoFocus
          />
        </Field>
        <Field label="描述" hint="一句话说明这个知识库收录什么内容">
          <TextArea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={3}
            placeholder="例如：公司制度、考勤与假期规定"
          />
        </Field>
        <Field
          label="模式"
          hint={
            mode === 'wiki'
              ? 'Wiki 增强：文档入库后需手动「重建 Wiki」，才会生成结构化页面。'
              : '基础分块：只做解析与分块索引，问答按片段检索。'
          }
        >
          <Select value={mode} onChange={(e) => setMode(e.target.value as KbMode)}>
            <option value="basic">基础分块</option>
            <option value="wiki">Wiki 增强</option>
          </Select>
        </Field>
      </Modal>

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title="删除知识库"
        message={
          <>
            <div>
              确认删除知识库「<strong>{deleteTarget?.name}</strong>」？
            </div>
            <div style={{ marginTop: 8 }}>
              将同时删除该库的{' '}
              <strong>{formatNumber(deleteTarget?.stats?.doc_count)} 个文档</strong>、
              <strong>{formatNumber(deleteTarget?.stats?.chunk_count)} 个分块索引</strong>
              {deleteTarget?.stats?.wiki_page_count
                ? `、${formatNumber(deleteTarget.stats.wiki_page_count)} 个 Wiki 页面`
                : ''}
              ，以及磁盘上的原始文件。
            </div>
            <div style={{ marginTop: 8, color: 'var(--c-danger)' }}>
              此操作不可恢复，相关授权与检索能力一并失效。
            </div>
          </>
        }
        confirmText="确认删除"
        danger
        busy={deleteBusy}
        onConfirm={() => void submitDelete()}
        onCancel={() => setDeleteTarget(null)}
      />
    </>
  )
}

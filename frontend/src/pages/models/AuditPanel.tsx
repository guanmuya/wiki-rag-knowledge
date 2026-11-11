import { useCallback, useEffect, useState } from 'react'
import { settingsApi } from '../../api'
import type { AuditEntry } from '../../types'
import { AUDIT_ACTION_LABEL } from '../../types'
import { formatTime } from '../../utils'
import { Badge, Button, EmptyState, ErrorState, SkeletonLines } from '../../components/ui'

export const AUDIT_PAGE_SIZE = 20

/** 审计日志的按页加载状态；切到审计页时才拉第一页。 */
export function useAudit(tab: 'models' | 'audit') {
  // ---- 审计日志
  const [auditItems, setAuditItems] = useState<AuditEntry[]>([])
  const [auditTotal, setAuditTotal] = useState(0)
  const [auditPage, setAuditPage] = useState(1)
  const [auditLoading, setAuditLoading] = useState(false)
  const [auditError, setAuditError] = useState<unknown>(null)
  const [auditLoaded, setAuditLoaded] = useState(false)

  const loadAudit = useCallback(
    async (page: number) => {
      setAuditLoading(true)
      setAuditError(null)
      try {
        const data = await settingsApi.audit(page, AUDIT_PAGE_SIZE)
        setAuditItems(data.items ?? [])
        setAuditTotal(data.total ?? 0)
        setAuditPage(data.page ?? page)
      } catch (err) {
        setAuditError(err)
        setAuditItems([])
      } finally {
        setAuditLoading(false)
        setAuditLoaded(true)
      }
    },
    [],
  )

  useEffect(() => {
    if (tab === 'audit' && !auditLoaded) void loadAudit(1)
  }, [tab, auditLoaded, loadAudit])

  return {
    auditItems,
    auditTotal,
    auditPage,
    auditLoading,
    auditError,
    auditLoaded,
    loadAudit,
  }
}

export function AuditPanel({
  items,
  total,
  page,
  pageSize,
  loading,
  error,
  onPage,
}: {
  items: AuditEntry[]
  total: number
  page: number
  pageSize: number
  loading: boolean
  error: unknown
  onPage: (page: number) => void
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize))

  return (
    <div className="card">
      <div className="card-head">
        <h3>审计日志</h3>
        <span className="tiny muted">共 {total} 条</span>
      </div>
      <div className="card-body card-body-flush">
        {loading ? (
          <div style={{ padding: 16 }}>
            <SkeletonLines rows={8} />
          </div>
        ) : error ? (
          <ErrorState error={error} onRetry={() => onPage(page)} />
        ) : items.length === 0 ? (
          <EmptyState
            title="暂无审计记录"
            hint="登录、上传文档、调整授权、修改模型等操作都会记录在这里。"
          />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th style={{ width: 150 }}>时间</th>
                  <th style={{ width: 140 }}>用户</th>
                  <th style={{ width: 120 }}>操作</th>
                  <th style={{ width: 200 }}>对象</th>
                  <th>详情</th>
                </tr>
              </thead>
              <tbody>
                {items.map((row) => (
                  <tr key={row.id}>
                    <td className="small muted nowrap">{formatTime(row.created_at)}</td>
                    <td className="small">{row.username || '—'}</td>
                    <td>
                      <Badge tone="muted">{AUDIT_ACTION_LABEL[row.action] ?? row.action}</Badge>
                    </td>
                    <td className="small truncate" style={{ maxWidth: 200 }} title={row.target}>
                      {row.target || '—'}
                    </td>
                    <td className="small muted" style={{ wordBreak: 'break-word' }}>
                      {row.detail || '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <div className="pager">
        <span className="tiny muted">
          第 {page} / {pages} 页 · 每页 {pageSize} 条
        </span>
        <div className="spacer" />
        <Button size="sm" disabled={page <= 1 || loading} onClick={() => onPage(1)}>
          首页
        </Button>
        <Button size="sm" disabled={page <= 1 || loading} onClick={() => onPage(page - 1)}>
          上一页
        </Button>
        <Button size="sm" disabled={page >= pages || loading} onClick={() => onPage(page + 1)}>
          下一页
        </Button>
        <Button size="sm" disabled={page >= pages || loading} onClick={() => onPage(pages)}>
          末页
        </Button>
      </div>
    </div>
  )
}

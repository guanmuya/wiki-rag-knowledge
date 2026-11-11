import type { Provider } from '../../types'
import { formatTime } from '../../utils'
import { Badge, Button, EmptyState } from '../../components/ui'
import { PROTOCOL_HINT, PROTOCOL_SHORT, protocolOf } from './constants'

export function ProviderTable({
  providers,
  onCreate,
  onDiscover,
  onEdit,
  onDelete,
}: {
  providers: Provider[]
  onCreate: () => void
  onDiscover: (provider: Provider) => void
  onEdit: (provider: Provider) => void
  onDelete: (provider: Provider) => void
}) {
  return (
    <div className="card">
      <div className="card-head">
        <h3>供应商</h3>
        <span className="tiny muted">{providers.length} 个</span>
      </div>
      <div className="card-body card-body-flush">
        {providers.length === 0 ? (
          <EmptyState
            title="还没有配置供应商"
            hint="先添加供应商的 API 地址和 Key，再添加具体模型。"
            action={
              <Button size="sm" variant="primary" onClick={onCreate}>
                添加供应商
              </Button>
            }
          />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>名称</th>
                  <th>协议</th>
                  <th>Base URL</th>
                  <th>API Key</th>
                  <th>状态</th>
                  <th>创建时间</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {providers.map((p) => (
                  <tr key={p.id}>
                    <td className="strong">{p.name}</td>
                    <td>
                      <span title={PROTOCOL_HINT[protocolOf(p)]}>
                        <Badge tone={protocolOf(p) === 'openai' ? 'muted' : 'info'}>
                          {PROTOCOL_SHORT[protocolOf(p)]}
                        </Badge>
                      </span>
                    </td>
                    <td className="mono small">{p.base_url}</td>
                    <td>
                      {p.has_key ? (
                        <span className="mono small">{p.api_key_masked || '****'}</span>
                      ) : (
                        <span className="small muted">未配置</span>
                      )}
                    </td>
                    <td>
                      <Badge tone={p.enabled ? 'success' : 'muted'}>
                        {p.enabled ? '启用' : '停用'}
                      </Badge>
                    </td>
                    <td className="small muted nowrap">{formatTime(p.created_at)}</td>
                    <td className="actions">
                      <div className="table-actions">
                        <button
                          type="button"
                          className="btn-link"
                          onClick={() => onDiscover(p)}
                        >
                          探测模型
                        </button>
                        <button
                          type="button"
                          className="btn-link"
                          onClick={() => onEdit(p)}
                        >
                          编辑
                        </button>
                        <button
                          type="button"
                          className="btn-link danger"
                          onClick={() => onDelete(p)}
                        >
                          删除
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

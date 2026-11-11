import type { ModelConfig, ModelPurpose, ModelTestResult } from '../../types'
import { formatMs, formatTime } from '../../utils'
import {
  IconCheck,
  IconChevronDown,
  IconChevronRight,
  IconClose,
  IconPlus,
} from '../../components/icons'
import { Badge, Button, EmptyState } from '../../components/ui'
import { PURPOSE_ICON, PURPOSE_LABEL, PURPOSE_ORDER } from './constants'

export function ModelGroups({
  models,
  collapsedGroups,
  onToggleGroup,
  onCreate,
  onEdit,
  onDelete,
  onSetDefault,
  onToggle,
  onTest,
  testing,
  testResults,
}: {
  models: ModelConfig[]
  collapsedGroups: Record<ModelPurpose, boolean>
  onToggleGroup: (purpose: ModelPurpose) => void
  onCreate: (purpose: ModelPurpose) => void
  onEdit: (model: ModelConfig) => void
  onDelete: (model: ModelConfig) => void
  onSetDefault: (model: ModelConfig) => void
  onToggle: (model: ModelConfig) => void
  onTest: (model: ModelConfig) => void
  testing: number | null
  testResults: Record<number, ModelTestResult>
}) {
  return (
    <div className="card">
      <div className="card-head">
        <h3>模型</h3>
        <span className="tiny muted">{models.length} 个</span>
      </div>
      <div className="card-body">
        {models.length === 0 ? (
          <EmptyState
            title="还没有配置模型"
            hint="按用途添加：对话模型（llm）、向量模型（embedding）、视觉模型（vision）。"
          />
        ) : (
          PURPOSE_ORDER.map((purpose) => {
            const group = models.filter((m) => m.purpose === purpose)
            if (group.length === 0) return null
            const PurposeIcon = PURPOSE_ICON[purpose]
            const collapsed = collapsedGroups[purpose]
            return (
              <div className={`model-group${collapsed ? ' is-collapsed' : ''}`} key={purpose}>
                <div className="model-group-title">
                  <button
                    type="button"
                    className="model-group-toggle"
                    onClick={() => onToggleGroup(purpose)}
                    aria-expanded={!collapsed}
                    title={collapsed ? '展开该用途的模型' : '折叠该用途的模型'}
                  >
                    <span className="model-group-chevron">
                      {collapsed ? <IconChevronRight size={16} /> : <IconChevronDown size={16} />}
                    </span>
                    <span className="model-group-icon">
                      <PurposeIcon size={16} />
                    </span>
                    <span className="model-group-name">{PURPOSE_LABEL[purpose]}</span>
                    <span className="model-group-count">{group.length} 个</span>
                  </button>
                  <button
                    type="button"
                    className="model-group-add"
                    onClick={() => onCreate(purpose)}
                  >
                    <IconPlus size={14} />
                    添加模型
                  </button>
                </div>
                <div className="table-wrap" hidden={collapsed}>
                  {/* 三个用途的表共用同一套列宽（.model-table 固定布局）：
                      维度列在非向量表里显示「—」，这样跨表的列头才对得齐。 */}
                  <table className="table model-table">
                    <colgroup>
                      <col />
                      <col className="model-col-provider" />
                      <col className="model-col-dim" />
                      <col className="model-col-default" />
                      <col className="model-col-enabled" />
                      <col className="model-col-tested" />
                      <col className="model-col-actions" />
                    </colgroup>
                    <thead>
                      <tr>
                        <th>模型</th>
                        <th>供应商</th>
                        <th>维度</th>
                        <th>默认</th>
                        <th>启用</th>
                        <th>最近测试</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {group.map((m) => {
                        const res = testResults[m.id]
                        return (
                          <tr key={m.id}>
                            <td>
                              <div className="strong">{m.label || m.model}</div>
                              <div className="mono tiny muted">{m.model}</div>
                            </td>
                            <td className="small">{m.provider_name}</td>
                            <td className="small">{m.embed_dim || '—'}</td>
                            <td>
                              {m.is_default ? (
                                <Badge tone="info">默认</Badge>
                              ) : (
                                <button
                                  type="button"
                                  className="btn-link"
                                  onClick={() => onSetDefault(m)}
                                >
                                  设为默认
                                </button>
                              )}
                            </td>
                            <td>
                              <label className={`switch${m.enabled ? ' on' : ''}`}>
                                <input
                                  type="checkbox"
                                  checked={m.enabled}
                                  aria-label={`启用 ${m.label || m.model}`}
                                  onChange={() => onToggle(m)}
                                />
                              </label>
                            </td>
                            <td>
                              <div className="row" style={{ gap: 8 }}>
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  loading={testing === m.id}
                                  onClick={() => onTest(m)}
                                >
                                  连接测试
                                </Button>
                                {res ? (
                                  <span
                                    className={`test-result ${
                                      res.ok ? 'test-ok' : 'test-fail'
                                    }`}
                                  >
                                    {res.ok ? <IconCheck size={12} /> : <IconClose size={12} />} {res.message}
                                    {res.ok ? ` · ${formatMs(res.latency_ms)}` : ''}
                                  </span>
                                ) : m.last_test_at ? (
                                  <span
                                    className={`test-result ${
                                      m.last_test_ok ? 'test-ok' : 'test-fail'
                                    }`}
                                    title={m.last_test_message}
                                  >
                                    {m.last_test_ok ? <IconCheck size={12} /> : <IconClose size={12} />} {formatTime(m.last_test_at)}
                                  </span>
                                ) : (
                                  <span className="tiny muted">未测试</span>
                                )}
                              </div>
                            </td>
                            <td className="actions">
                              <div className="table-actions">
                                <button
                                  type="button"
                                  className="btn-link"
                                  onClick={() => onEdit(m)}
                                >
                                  编辑
                                </button>
                                <button
                                  type="button"
                                  className="btn-link danger"
                                  onClick={() => onDelete(m)}
                                >
                                  删除
                                </button>
                              </div>
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}

import { useState } from 'react'
import { useToast } from '../../auth'
import { settingsApi } from '../../api'
import type { ModelPurpose, Provider } from '../../types'
import { errorText } from '../../utils'
import { IconAlert } from '../../components/icons'
import { Button, EmptyState, Modal, Select, Spinner } from '../../components/ui'
import type { DiscoverRow } from './constants'

/** 探测供应商模型列表并批量导入；弹窗开关也在这里，跟着页面实例走。 */
export function useDiscover({
  load,
  loadEmbed,
}: {
  load: () => Promise<void>
  loadEmbed: () => Promise<void>
}) {
  const { push } = useToast()

  // ---- 探测模型
  const [discoverTarget, setDiscoverTarget] = useState<Provider | null>(null)
  const [discoverRows, setDiscoverRows] = useState<DiscoverRow[]>([])
  const [discoverSource, setDiscoverSource] = useState<'api' | 'builtin'>('api')
  const [discoverError, setDiscoverError] = useState('')
  const [discoverBusy, setDiscoverBusy] = useState(false)
  const [importBusy, setImportBusy] = useState(false)

  /** 打开探测弹窗并立即发起一次探测。 */
  const openDiscover = async (p: Provider) => {
    setDiscoverTarget(p)
    setDiscoverRows([])
    setDiscoverError('')
    setDiscoverSource('api')
    setDiscoverBusy(true)
    try {
      const res = await settingsApi.discoverModels(p.id)
      setDiscoverSource(res.source)
      setDiscoverError(res.error || '')
      // 模型列表可能包括语音、审核等无法用于问答的模型；让用户确认用途后再导入。
      setDiscoverRows(
        (res.items ?? []).map((item) => ({
          ...item,
          checked: false,
          purpose: item.purpose_hint,
        })),
      )
    } catch (err) {
      // 后端承诺不 500，这里兜的是网络层（断网/401 等）
      setDiscoverError(errorText(err))
    } finally {
      setDiscoverBusy(false)
    }
  }

  const closeDiscover = () => {
    setDiscoverTarget(null)
    setDiscoverRows([])
    setDiscoverError('')
  }

  const toggleDiscoverRow = (model: string) => {
    setDiscoverRows((prev) =>
      prev.map((r) => (r.model === model ? { ...r, checked: !r.checked } : r)),
    )
  }

  const setDiscoverPurpose = (model: string, purpose: ModelPurpose) => {
    setDiscoverRows((prev) => prev.map((r) => (r.model === model ? { ...r, purpose } : r)))
  }

  const toggleDiscoverAll = (checked: boolean) => {
    setDiscoverRows((prev) => prev.map((r) => ({ ...r, checked })))
  }

  const doImport = async () => {
    if (!discoverTarget) return
    const picked = discoverRows.filter((r) => r.checked)
    if (picked.length === 0) {
      setDiscoverError('请至少勾选一个模型')
      return
    }
    setImportBusy(true)
    setDiscoverError('')
    try {
      const res = await settingsApi.importModels(
        discoverTarget.id,
        picked.map((r) => ({ model: r.model, purpose: r.purpose, label: r.label })),
      )
      const created = res.created_count ?? res.created?.length ?? 0
      const skipped = res.skipped_count ?? res.skipped?.length ?? 0
      push(
        skipped
          ? `已导入 ${created} 个模型，跳过 ${skipped} 个（已存在）`
          : `已导入 ${created} 个模型`,
        created > 0 ? 'success' : 'info',
      )
      closeDiscover()
      await load()
      await loadEmbed()
    } catch (err) {
      setDiscoverError(errorText(err))
    } finally {
      setImportBusy(false)
    }
  }

  return {
    discoverTarget,
    discoverRows,
    discoverSource,
    discoverError,
    discoverBusy,
    importBusy,
    toggleDiscoverRow,
    setDiscoverPurpose,
    toggleDiscoverAll,
    openDiscover,
    closeDiscover,
    doImport,
  }
}

export type Discover = ReturnType<typeof useDiscover>

export function DiscoverModal({ discover }: { discover: Discover }) {
  const {
    discoverTarget,
    discoverRows,
    discoverSource,
    discoverError,
    discoverBusy,
    importBusy,
    toggleDiscoverRow,
    setDiscoverPurpose,
    toggleDiscoverAll,
    closeDiscover,
    doImport,
  } = discover

  return (
    <Modal
      open={Boolean(discoverTarget)}
      title={`探测模型 · ${discoverTarget?.name ?? ''}`}
      onClose={closeDiscover}
      width={640}
      footer={
        <>
          <Button variant="ghost" onClick={closeDiscover} disabled={importBusy}>
            取消
          </Button>
          <Button
            variant="primary"
            loading={importBusy}
            disabled={discoverBusy || discoverRows.every((r) => !r.checked)}
            onClick={() => void doImport()}
          >
            导入所选（{discoverRows.filter((r) => r.checked).length}）
          </Button>
        </>
      }
    >
      {discoverBusy ? (
        <div className="state-block inline">
          <Spinner size={14} /> 正在向供应商请求模型列表…
        </div>
      ) : (
        <>
          {discoverError ? (
            <div className="insufficient-banner" style={{ marginBottom: 12 }}>
              <span className="banner-icon"><IconAlert size={15} /></span>
              <div style={{ flex: 1 }}>
                探测失败：{discoverError}
                <div className="tiny muted" style={{ marginTop: 4 }}>
                  可改用「+ 模型」手动填写模型名；接口不支持的供应商不会影响已有配置。
                </div>
              </div>
            </div>
          ) : null}
          {discoverSource === 'builtin' ? (
            <div className="insufficient-banner" style={{ marginBottom: 12 }}>
              <span>ℹ</span>
              <div style={{ flex: 1 }}>
                该供应商没有可用的模型列表接口，下面显示的是内置常用清单，请核对后导入。
              </div>
            </div>
          ) : null}

          {discoverRows.length === 0 ? (
            <EmptyState
              title="没有探测到可用模型"
              hint="确认 Base URL、API Key 与协议是否匹配；也可以直接手动添加模型。"
            />
          ) : (
            <>
              <p className="small muted" style={{ marginBottom: 10 }}>
                供应商列表可能包含不支持问答或向量的模型，请核对用途后勾选。若当前用途未设置默认模型，导入的新模型可能自动用于后续请求；可先关闭此窗口并设置默认模型。
              </p>
              <div className="row" style={{ gap: 10, marginBottom: 8 }}>
                <button
                  type="button"
                  className="btn-link"
                  onClick={() => toggleDiscoverAll(true)}
                >
                  全选
                </button>
                <button
                  type="button"
                  className="btn-link"
                  onClick={() => toggleDiscoverAll(false)}
                >
                  全不选
                </button>
                <span className="spacer" />
                <span className="tiny muted">共 {discoverRows.length} 个</span>
              </div>
              <div className="table-wrap table-wrap--scroll" style={{ maxHeight: 380 }}>
                <table className="table">
                  <thead>
                    <tr>
                      <th style={{ width: 40 }} />
                      <th>模型</th>
                      <th style={{ width: 150 }}>用途</th>
                    </tr>
                  </thead>
                  <tbody>
                    {discoverRows.map((r) => (
                      <tr key={r.model}>
                        <td>
                          <input
                            type="checkbox"
                            checked={r.checked}
                            onChange={() => toggleDiscoverRow(r.model)}
                            aria-label={`选择 ${r.model}`}
                          />
                        </td>
                        <td>
                          <div className="mono small">{r.model}</div>
                          {r.label && r.label !== r.model ? (
                            <div className="tiny muted">{r.label}</div>
                          ) : null}
                          {r.already_imported ? (
                            <div className="tiny muted">已存在同用途配置</div>
                          ) : null}
                        </td>
                        <td>
                          <Select
                            value={r.purpose}
                            onChange={(e) =>
                              setDiscoverPurpose(r.model, e.target.value as ModelPurpose)
                            }
                            aria-label={`${r.model} 的用途`}
                            style={{ minWidth: 120 }}
                          >
                            <option value="llm">对话 llm</option>
                            <option value="embedding">向量 embedding</option>
                            <option value="vision">视觉 vision</option>
                          </Select>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </>
      )}
    </Modal>
  )
}

import { useEffect, useState } from 'react'
import { pricingApi, type ModelPriceItem, type PricingCatalog } from '../../api'
import { Button, EmptyState, ErrorState, LoadingBlock, Modal } from '../../components/ui'
import { errorText } from '../../utils'
import { PURPOSE_LABEL } from '../models/constants'
import { ORIGIN_LABEL, ORIGIN_TONE, formatPrice } from './format'

/** 与后端 `schemas.ModelPriceIn` 的上限一致：拦明显的手误（把「每 1K」的价填进「每 1M」）。 */
const MAX_PRICE = 100_000
const PRICE_PATTERN = /^\d+(?:\.\d+)?$/

interface Draft {
  input: string
  output: string
}

function toDraft(item: ModelPriceItem): Draft {
  return {
    input: item.effective.input_per_1m === null ? '' : String(item.effective.input_per_1m),
    output: item.effective.output_per_1m === null ? '' : String(item.effective.output_per_1m),
  }
}

/** 合法单价：非负数字、不超过上限。空串、负数、科学计数法都不合法。 */
function parsePrice(text: string): number | null {
  const trimmed = text.trim()
  if (!PRICE_PATTERN.test(trimmed)) return null
  const value = Number(trimmed)
  if (!Number.isFinite(value) || value > MAX_PRICE) return null
  return value
}

/**
 * 模型单价面板。
 *
 * 入口放在用量页的费用卡里，因为「这个金额是怎么算出来的」和「单价是多少」必须
 * 同屏回答。面板只改单价、不动模型配置，保存后回调父级重拉一次用量，金额卡立即
 * 反映新价；没有模型配置时不编造示例行，直接给空状态指向模型设置页。
 */
export default function PricingEditor({
  open,
  onClose,
  onChanged,
}: {
  open: boolean
  onClose: () => void
  onChanged: () => void
}) {
  const [catalog, setCatalog] = useState<PricingCatalog | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [revision, setRevision] = useState(0)
  const [drafts, setDrafts] = useState<Record<number, Draft>>({})
  const [busyId, setBusyId] = useState<number | null>(null)
  const [notice, setNotice] = useState('')
  const [rowError, setRowError] = useState<{ id: number; text: string } | null>(null)

  useEffect(() => {
    if (!open) return
    let active = true
    setLoading(true)
    setError(null)
    setNotice('')
    setRowError(null)
    void pricingApi.list()
      .then((result) => {
        if (!active) return
        const next: Record<number, Draft> = {}
        result.items.forEach((item) => { next[item.model_config_id] = toDraft(item) })
        setCatalog(result)
        setDrafts(next)
      })
      .catch((reason: unknown) => {
        if (active) setError(reason)
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => { active = false }
  }, [open, revision])

  const applyUpdated = (updated: ModelPriceItem) => {
    setCatalog((prev) => (prev
      ? { ...prev, items: prev.items.map((row) => (
          row.model_config_id === updated.model_config_id ? updated : row
        )) }
      : prev))
    setDrafts((prev) => ({
      ...prev,
      // 「恢复参考价」之后 user 为空：把输入框清空，别让内置价的数字看起来像
      // 一条待保存的修改——否则一次误点就会把内置价固化成覆盖价，从此不再跟随目录。
      [updated.model_config_id]: updated.effective.origin === 'user'
        ? toDraft(updated)
        : { input: '', output: '' },
    }))
  }

  const save = async (item: ModelPriceItem) => {
    const draft = drafts[item.model_config_id] ?? toDraft(item)
    const input = parsePrice(draft.input)
    const output = parsePrice(draft.output)
    if (input === null || output === null) return
    setBusyId(item.model_config_id)
    setRowError(null)
    setNotice('')
    try {
      const updated = await pricingApi.set(item.model_config_id, {
        input_per_1m: input,
        output_per_1m: output,
      })
      applyUpdated(updated)
      setNotice(`已保存「${updated.label}」的单价，金额会按新单价重新估算`)
      onChanged()
    } catch (reason: unknown) {
      setRowError({ id: item.model_config_id, text: errorText(reason) })
    } finally {
      setBusyId(null)
    }
  }

  const clear = async (item: ModelPriceItem) => {
    setBusyId(item.model_config_id)
    setRowError(null)
    setNotice('')
    try {
      const updated = await pricingApi.clear(item.model_config_id)
      applyUpdated(updated)
      setNotice(`「${updated.label}」已恢复为${
        updated.effective.origin === 'builtin' ? '内置参考价' : '未计价'
      }`)
      onChanged()
    } catch (reason: unknown) {
      setRowError({ id: item.model_config_id, text: errorText(reason) })
    } finally {
      setBusyId(null)
    }
  }

  const renderRow = (item: ModelPriceItem) => {
    const draft = drafts[item.model_config_id] ?? toDraft(item)
    const isEmbedding = item.purpose === 'embedding'
    const input = parsePrice(draft.input)
    const output = parsePrice(draft.output)
    const invalid = !isEmbedding && (input === null || output === null)
    const { effective } = item
    const tone = ORIGIN_TONE[effective.origin]
    const badgeClass = `badge badge-${tone} pricing-origin`
    const badgeTitle = effective.verified_at
      ? `${effective.source || ORIGIN_LABEL[effective.origin]}，${effective.verified_at} 查证`
      : effective.source || '没有找到公开参考价'
    // 空着的时候保存是灰的：给一句说明，并用 aria-describedby 让读屏也知道原因。
    const emptyDraft = !isEmbedding && (draft.input.trim() === '' || draft.output.trim() === '')
    const hintId = `pricing-hint-${item.model_config_id}`

    return (
      <div className="pricing-row" key={item.model_config_id} data-model-id={item.model_config_id}>
        <div className="pricing-row-head">
          <div className="pricing-row-title">
            <span className="strong">{item.label}</span>
            <span className="mono tiny muted">{item.model}</span>
          </div>
          <div className="pricing-row-meta">
            <span>{item.provider || '未知供应商'}</span>
            <span>·</span>
            <span>{PURPOSE_LABEL[item.purpose]}</span>
            {effective.source_url ? (
              <a
                className={badgeClass}
                href={effective.source_url}
                target="_blank"
                rel="noreferrer"
                title={badgeTitle}
              >
                {ORIGIN_LABEL[effective.origin]}
              </a>
            ) : (
              <span className={badgeClass} title={badgeTitle}>
                {ORIGIN_LABEL[effective.origin]}
              </span>
            )}
          </div>
        </div>

        <p className="pricing-effective">
          {effective.origin === 'none'
            ? (isEmbedding
                ? '未计价：没有参考价，向量 Token 本来就只做展示'
                : '未计价：没有公开参考价，可以自己填价')
            : `生效单价：输入 ${formatPrice(effective.input_per_1m, effective.currency)} · 输出 ${formatPrice(effective.output_per_1m, effective.currency)}`}
        </p>

        <div className="pricing-fields">
          {isEmbedding ? (
            <p className="pricing-readonly">向量 Token 不参与估算，这里只显示参考价。</p>
          ) : (
            <>
              <label className="pricing-field">
                <span className="tiny muted">输入单价</span>
                <input
                  className="input"
                  inputMode="decimal"
                  placeholder="0.00"
                  value={draft.input}
                  aria-label={`${item.label} 输入单价`}
                  aria-invalid={input === null && draft.input.trim() !== ''}
                  aria-describedby={emptyDraft ? hintId : undefined}
                  onChange={(event) => setDrafts((prev) => ({
                    ...prev,
                    [item.model_config_id]: { ...draft, input: event.target.value },
                  }))}
                />
                {input === null && draft.input.trim() !== '' ? (
                  <span className="field-error">填 0 到 100000 之间的数字</span>
                ) : null}
              </label>

              <label className="pricing-field">
                <span className="tiny muted">输出单价</span>
                <input
                  className="input"
                  inputMode="decimal"
                  placeholder="0.00"
                  value={draft.output}
                  aria-label={`${item.label} 输出单价`}
                  aria-invalid={output === null && draft.output.trim() !== ''}
                  aria-describedby={emptyDraft ? hintId : undefined}
                  onChange={(event) => setDrafts((prev) => ({
                    ...prev,
                    [item.model_config_id]: { ...draft, output: event.target.value },
                  }))}
                />
                {output === null && draft.output.trim() !== '' ? (
                  <span className="field-error">填 0 到 100000 之间的数字</span>
                ) : null}
              </label>
            </>
          )}

          <div className="pricing-row-actions">
            {isEmbedding ? null : (
              <Button
                size="sm"
                variant="primary"
                loading={busyId === item.model_config_id}
                disabled={invalid}
                onClick={() => void save(item)}
              >
                保存
              </Button>
            )}
            {item.user ? (
              <Button
                size="sm"
                variant="ghost"
                disabled={busyId === item.model_config_id}
                onClick={() => void clear(item)}
              >
                恢复参考价
              </Button>
            ) : null}
          </div>
        </div>

        {emptyDraft ? (
          <p className="field-hint pricing-hint" id={hintId}>
            填好输入和输出单价（0 到 100000）才能保存；填 0 表示这个渠道不额外计费。
          </p>
        ) : null}

        {rowError && rowError.id === item.model_config_id ? (
          <p className="pricing-row-error">{rowError.text}</p>
        ) : null}
      </div>
    )
  }

  return (
    <Modal
      open={open}
      title="模型单价"
      width={860}
      onClose={onClose}
      footer={<Button variant="ghost" onClick={onClose}>关闭</Button>}
    >
      {loading ? (
        <LoadingBlock text="正在加载单价…" />
      ) : error ? (
        <ErrorState error={error} onRetry={() => setRevision((value) => value + 1)} />
      ) : catalog ? (
        <>
          <p className="pricing-note">{catalog.note}</p>
          {notice ? <p className="pricing-notice" role="status">{notice}</p> : null}
          {catalog.items.length === 0 ? (
            <EmptyState
              title="还没有模型配置"
              hint="先到「模型设置」添加模型，再回来为它填单价；没有单价的调用会如实显示为未计价。"
            />
          ) : (
            <div className="pricing-list">{catalog.items.map(renderRow)}</div>
          )}
          {catalog.sources.length > 0 ? (
            <div className="pricing-sources">
              <span className="tiny strong">价格出处</span>
              <ul>
                {catalog.sources.map((source) => (
                  <li key={source.id}>
                    <a href={source.url} target="_blank" rel="noreferrer">{source.name}</a>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </>
      ) : null}
    </Modal>
  )
}

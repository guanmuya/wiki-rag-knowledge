import type { UsageTotals } from '../../api'
import { Badge } from '../../components/ui'
import { formatNumber } from '../../utils'
import { formatAmount } from './format'

/**
 * 六张指标卡。
 *
 * 每一张都写清「这个数字是怎么来的」：Token 卡说明向量不参与金额，调用卡给出
 * 在线/离线/未区分的拆分，费用卡紧跟「估算」徽标与已计价/未计价行数，未计价卡
 * 说明还有多少次调用连 Token 都没留下。读者不需要滚到页脚才知道 $ 不是账单。
 */
export default function UsageMetrics({
  totals,
  currency,
  onManagePricing,
}: {
  totals: UsageTotals
  currency: string
  onManagePricing: () => void
}) {
  const cost = totals.cost
  const unknownCalls = Math.max(0, totals.calls - totals.live_calls - totals.offline_calls)
  // 「没有已计价的调用」与「免费」是两件事：前者是没找到单价，后者是价格为 0。
  const priced = cost.priced_records > 0

  return (
    <section className="usage-metrics" aria-label="用量汇总">
      <div className="usage-summary">
        <div className="usage-metric">
          <div className="usage-metric-label">输入 Token</div>
          <strong>{formatNumber(totals.prompt_tokens)}</strong>
          <small>生成与视觉调用的输入量</small>
        </div>

        <div className="usage-metric">
          <div className="usage-metric-label">输出 Token</div>
          <strong>{formatNumber(totals.completion_tokens)}</strong>
          <small>由供应商响应提供</small>
        </div>

        <div className="usage-metric">
          <div className="usage-metric-label">向量 Token</div>
          <strong>{formatNumber(totals.embed_tokens)}</strong>
          <small>一律按未计价，不参与金额</small>
        </div>

        <div className="usage-metric">
          <div className="usage-metric-label">调用与操作</div>
          <strong>{formatNumber(totals.calls)}</strong>
          <small>
            在线 {formatNumber(totals.live_calls)} · 离线 {formatNumber(totals.offline_calls)}
            {unknownCalls > 0 ? ` · 未区分 ${formatNumber(unknownCalls)}` : ''}
          </small>
        </div>

        <div className="usage-metric">
          <div className="usage-metric-label">
            估算费用
            <Badge tone="muted">估算</Badge>
            {cost.unpriced_records > 0 ? <Badge tone="warn">含未计价</Badge> : null}
          </div>
          <strong>{formatAmount(cost.amount, currency)}</strong>
          <div className="usage-metric-foot">
            <small>
              {priced
                ? `已计价 ${formatNumber(cost.priced_records)} 次 · 未计价 ${formatNumber(cost.unpriced_records)} 次`
                : '没有已计价的调用'}
            </small>
            <button type="button" className="btn-link" onClick={onManagePricing}>
              管理单价
            </button>
          </div>
        </div>

        <div className="usage-metric">
          <div className="usage-metric-label">未计价 Token</div>
          <strong>{formatNumber(cost.unpriced_tokens)}</strong>
          <small>无 Token 记录 {formatNumber(totals.unmetered_records)} 次</small>
        </div>
      </div>

      {totals.unmetered_records > 0 ? (
        <p className="usage-metrics-alert" role="note">
          有 {formatNumber(totals.unmetered_records)} 次调用没有留下 Token 记录（供应商没回报用量，或本机 CLI
          只回文本）：它们计入「调用与操作」，但不进 Token 合计与金额，别把 0 Token 当成「没有调用」。
        </p>
      ) : null}
    </section>
  )
}

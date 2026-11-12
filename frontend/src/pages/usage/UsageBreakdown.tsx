import type { UsageSummary, UsageTotals } from '../../api'
import { formatNumber } from '../../utils'
import { formatAmount } from './format'

/** 来源汇总与「个人 Agent 按核心」两张表；两者共用同一组列。 */
function Row({ label, bucket, currency }: { label: string; bucket: UsageTotals; currency: string }) {
  return (
    <tr>
      <td><span className="strong">{label}</span></td>
      <td>{formatNumber(bucket.calls)}</td>
      <td>{formatNumber(bucket.prompt_tokens)}</td>
      <td>{formatNumber(bucket.completion_tokens)}</td>
      <td>{formatNumber(bucket.embed_tokens)}</td>
      <td>{formatNumber(bucket.total_tokens)}</td>
      <td>{formatAmount(bucket.cost.amount, currency)}</td>
      <td>{formatNumber(bucket.cost.unpriced_records)}</td>
    </tr>
  )
}

export default function UsageBreakdown({ data }: { data: UsageSummary }) {
  const currency = data.currency
  const columns = (first: string) => (
    <thead>
      <tr>
        <th>{first}</th>
        <th>调用与操作</th>
        <th>输入 Token</th>
        <th>输出 Token</th>
        <th>向量 Token</th>
        <th>Token 合计</th>
        <th>估算费用</th>
        <th>未计价次数</th>
      </tr>
    </thead>
  )

  return (
    <section className="card usage-breakdown" aria-labelledby="usage-sources-title">
      <div className="card-head">
        <h3 id="usage-sources-title">用量来源</h3>
        <span className="tiny muted">按知识库处理、问答与个人 Agent 分别汇总，跨多个知识库的问答只算一次</span>
      </div>
      <div className="table-wrap table-wrap--scroll">
        <table className="table usage-numeric">
          {columns('来源')}
          <tbody>
            {data.by_source.map((source) => (
              <Row key={source.source} label={source.label} bucket={source} currency={currency} />
            ))}
          </tbody>
        </table>
      </div>

      {data.by_core.length > 0 ? (
        <>
          <div className="card-head">
            <h3 id="usage-cores-title">个人 Agent 按核心</h3>
          </div>
          <div className="table-wrap table-wrap--scroll">
            <table className="table usage-numeric" aria-labelledby="usage-cores-title">
              {columns('核心')}
              <tbody>
                {data.by_core.map((core) => (
                  <Row key={core.core_id || 'unknown'} label={core.label} bucket={core} currency={currency} />
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : null}
    </section>
  )
}

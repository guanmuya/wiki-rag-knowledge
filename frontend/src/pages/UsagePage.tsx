import { useEffect, useState } from 'react'
import { usageApi, type UsageSummary } from '../api'
import { ErrorState, LoadingBlock } from '../components/ui'
import { formatNumber } from '../utils'
import './UsagePage.css'

type Period = 7 | 30 | 0

const PERIODS: Array<{ days: Period; label: string }> = [
  { days: 7, label: '近 7 天' },
  { days: 30, label: '近 30 天' },
  { days: 0, label: '全部' },
]

export default function UsagePage() {
  const [period, setPeriod] = useState<Period>(30)
  const [revision, setRevision] = useState(0)
  const [data, setData] = useState<UsageSummary | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let active = true
    setLoading(true)
    setError(null)
    void usageApi.summary(period).then((result) => {
      if (active) setData(result)
    }).catch((reason: unknown) => {
      if (active) setError(reason)
    }).finally(() => {
      if (active) setLoading(false)
    })
    return () => { active = false }
  }, [period, revision])

  const refresh = () => setRevision((value) => value + 1)
  const totals = data?.totals
  const points = data ? (period === 0 ? data.daily.slice(-30) : data.daily) : []
  const maxTokens = Math.max(1, ...points.map((point) => point.total_tokens))
  const labelStep = Math.max(1, Math.ceil(points.length / 6))
  const unknownCalls = totals ? Math.max(0, totals.calls - totals.live_calls - totals.offline_calls) : 0

  return (
    <div className="usage-page">
      <div className="page-head usage-heading">
        <div>
          <h1>用量概览</h1>
          <div className="sub">查看知识库处理和已保存问答的模型用量记录</div>
        </div>
        <button type="button" className="btn" onClick={refresh} disabled={loading}>刷新</button>
      </div>

      <div className="usage-toolbar">
        <div className="usage-periods" role="group" aria-label="选择统计时间范围">
          {PERIODS.map((option) => (
            <button
              type="button"
              key={option.days}
              className={period === option.days ? 'selected' : ''}
              aria-pressed={period === option.days}
              onClick={() => setPeriod(option.days)}
            >
              {option.label}
            </button>
          ))}
        </div>
        <span className="usage-timezone">按 UTC 日期统计</span>
      </div>

      {loading ? <div className="card"><LoadingBlock text="正在加载用量记录…" /></div> : error ? (
        <div className="card"><ErrorState error={error} onRetry={refresh} /></div>
      ) : data && totals ? (
        <>
          <div className="usage-summary" aria-label="用量汇总">
            <div className="usage-metric">
              <span>已记录 Token</span>
              <strong>{formatNumber(totals.total_tokens)}</strong>
              <small>生成 / 视觉输入、输出与向量之和</small>
            </div>
            <div className="usage-metric">
              <span>生成 / 视觉输入 Token</span>
              <strong>{formatNumber(totals.prompt_tokens)}</strong>
              <small>向量 Token {formatNumber(totals.embed_tokens)}</small>
            </div>
            <div className="usage-metric">
              <span>生成 / 视觉输出 Token</span>
              <strong>{formatNumber(totals.completion_tokens)}</strong>
              <small>由供应商响应提供</small>
            </div>
            <div className="usage-metric">
              <span>已记录调用与操作</span>
              <strong>{formatNumber(totals.calls)}</strong>
              <small>在线 {formatNumber(totals.live_calls)} · 离线 {formatNumber(totals.offline_calls)}{unknownCalls ? ` · 未区分 ${formatNumber(unknownCalls)}` : ''}</small>
            </div>
          </div>

          <section className="usage-panel" aria-labelledby="usage-trend-title">
            <div className="usage-panel-head">
              <div>
                <h2 id="usage-trend-title">Token 趋势</h2>
                <p>{period === 0 ? '每柱代表一个有记录的日期，间距不代表实际天数；图表最多显示最近 30 个日期，汇总和每日数据包含全部记录。' : '按记录日期展示生成和向量用量。'}</p>
              </div>
              <div className="usage-legend" aria-label="图例">
                <span><i className="usage-key usage-input" />生成 / 视觉输入</span>
                <span><i className="usage-key usage-output" />生成 / 视觉输出</span>
                <span><i className="usage-key usage-embed" />向量</span>
              </div>
            </div>
            {points.length > 0 && points.some((point) => point.total_tokens > 0) ? (
              <>
                <div className="usage-plot" role="img" aria-label="每日已记录 Token 趋势；具体数值可在下方每日数据中查看">
                  {points.map((point, index) => {
                    const description = `${point.date}：生成 / 视觉输入 ${formatNumber(point.prompt_tokens)}，生成 / 视觉输出 ${formatNumber(point.completion_tokens)}，向量 ${formatNumber(point.embed_tokens)} Token`
                    return (
                      <div className="usage-day" key={point.date}>
                        <div className="usage-bar-track" title={description}>
                          <span className="usage-bar usage-output" style={{ height: `${point.completion_tokens / maxTokens * 100}%` }} />
                          <span className="usage-bar usage-input" style={{ height: `${point.prompt_tokens / maxTokens * 100}%` }} />
                          <span className="usage-bar usage-embed" style={{ height: `${point.embed_tokens / maxTokens * 100}%` }} />
                        </div>
                        {period !== 0 && <span className="usage-date">{index % labelStep === 0 || index === points.length - 1 ? point.date.slice(5) : '\u00a0'}</span>}
                      </div>
                    )
                  })}
                </div>
                {period === 0 && <div className="usage-range"><span>{points[0].date}</span>{points.length > 1 && <span>{points[points.length - 1].date}</span>}</div>}
              </>
            ) : (
              <div className="usage-empty">{period === 0 ? '图表显示的日期没有已记录的 Token' : '这个时间范围内没有已记录的 Token'}</div>
            )}
            {points.length > 0 && (
              <details className="usage-daily">
                <summary>查看每日数据（UTC）</summary>
                <div className="usage-table-wrap">
                  <table className="usage-table">
                    <thead><tr><th>日期</th><th>调用与操作</th><th>输入</th><th>输出</th><th>向量</th><th>Token 合计</th></tr></thead>
                    <tbody>{data.daily.slice().reverse().map((point) => (
                      <tr key={point.date}>
                        <th scope="row">{point.date}</th>
                        <td>{formatNumber(point.calls)}</td>
                        <td>{formatNumber(point.prompt_tokens)}</td>
                        <td>{formatNumber(point.completion_tokens)}</td>
                        <td>{formatNumber(point.embed_tokens)}</td>
                        <td>{formatNumber(point.total_tokens)}</td>
                      </tr>
                    ))}</tbody>
                  </table>
                </div>
              </details>
            )}
          </section>

          <section className="usage-panel" aria-labelledby="usage-sources-title">
            <div className="usage-panel-head">
              <div>
                <h2 id="usage-sources-title">用量来源</h2>
                <p>按知识库处理和问答分别汇总，跨多个知识库的问答只计算一次。</p>
              </div>
            </div>
            <div className="usage-table-wrap">
              <table className="usage-table">
                <thead><tr><th>来源</th><th>调用与操作</th><th>生成 / 视觉输入</th><th>生成 / 视觉输出</th><th>向量 Token</th></tr></thead>
                <tbody>{data.by_source.map((source) => (
                  <tr key={source.source}>
                    <th scope="row">{source.label}</th>
                    <td>{formatNumber(source.calls)}</td>
                    <td>{formatNumber(source.prompt_tokens)}</td>
                    <td>{formatNumber(source.completion_tokens)}</td>
                    <td>{formatNumber(source.embed_tokens)}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          </section>

          <div className="usage-note">
            <strong>统计说明</strong>
            <p>此处仅汇总本应用保留的知识库处理流水和成功保存的问答；检索调试、连接测试以及未保存或失败的问答不在其中。离线操作不会产生供应商费用。</p>
            <p>供应商可能不返回 Token 数，失败或中断的调用也可能消耗用量，因此这些数字不能作为账单、余额或完整用量。处理按流水入账时间统计，问答按消息创建时间统计。</p>
          </div>
        </>
      ) : null}
    </div>
  )
}

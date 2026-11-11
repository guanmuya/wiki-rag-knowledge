import { useState } from 'react'
import type { UsageSummary, UsageTotals } from '../../api'
import { formatNumber } from '../../utils'
import { formatAmount } from './format'

type Point = UsageTotals & { date: string }
type Metric = 'tokens' | 'cost' | 'calls'
type ChartStyle = 'bar' | 'line'

const METRICS: Array<{ key: Metric; label: string }> = [
  { key: 'tokens', label: 'Token' },
  { key: 'cost', label: '费用' },
  { key: 'calls', label: '调用与操作' },
]

const STYLES: Array<{ key: ChartStyle; label: string }> = [
  { key: 'bar', label: '柱状' },
  { key: 'line', label: '折线' },
]

/** SVG 坐标系固定 720×220，外层按容器宽度缩放；窄屏由容器横向滚动（见 CSS）。 */
const W = 720
const H = 220
const PAD = { top: 16, right: 14, bottom: 26, left: 14 }
const PLOT_W = W - PAD.left - PAD.right
const PLOT_H = H - PAD.top - PAD.bottom

/** 堆叠顺序（自下而上）：向量 → 输入 → 输出；图例按输入、输出、向量的阅读顺序。 */
const STACK: Array<{ key: string; label: string; className: string; field: 'prompt_tokens' | 'completion_tokens' | 'embed_tokens' }> = [
  { key: 'embed', label: '向量', className: 'usage-chart-embed', field: 'embed_tokens' },
  { key: 'input', label: '生成 / 视觉输入', className: 'usage-chart-input', field: 'prompt_tokens' },
  { key: 'output', label: '生成 / 视觉输出', className: 'usage-chart-output', field: 'completion_tokens' },
]
const LEGEND_TOKENS = [STACK[1], STACK[2], STACK[0]]

const METRIC_LABEL: Record<Metric, string> = {
  tokens: 'Token',
  cost: '费用',
  calls: '调用与操作',
}

function valueOf(point: Point, metric: Metric): number {
  if (metric === 'cost') return point.cost.amount
  if (metric === 'calls') return point.calls
  return point.total_tokens
}

function describe(point: Point, metric: Metric, currency: string): string {
  if (metric === 'cost') return `${point.date}：估算费用 ${formatAmount(point.cost.amount, currency)}`
  if (metric === 'calls') return `${point.date}：调用与操作 ${formatNumber(point.calls)} 次`
  return `${point.date}：生成 / 视觉输入 ${formatNumber(point.prompt_tokens)}，生成 / 视觉输出 ${formatNumber(point.completion_tokens)}，向量 ${formatNumber(point.embed_tokens)} Token`
}

function emptyText(metric: Metric, all: boolean): string {
  const scope = all ? '图表显示的日期' : '这个时间范围内'
  if (metric === 'cost') return `${scope}没有可估算的金额`
  if (metric === 'calls') return `${scope}没有调用记录`
  return `${scope}没有已记录的 Token`
}

export default function UsageTrend({ data }: { data: UsageSummary }) {
  const [metric, setMetric] = useState<Metric>('tokens')
  const [style, setStyle] = useState<ChartStyle>('bar')

  // 「全部」看的是这批数据本身的时间范围，而不是当前选中的档位：切档位时
  // 页面会保留上一档的数据直到新请求回来，用 period 判断会让「近 7 天」
  // 短暂（或请求失败时一直）画出几百个旧数据点。
  const all = data.period_days === 0
  // `days=0` 时后端返回全部日期：图上只画最近 30 个有记录的日期，避免几百根柱子挤成一团。
  const points = all ? data.daily.slice(-30) : data.daily
  const currency = data.currency
  const maxValue = Math.max(1e-9, ...points.map((point) => valueOf(point, metric)))
  const slot = PLOT_W / Math.max(1, points.length)
  const barWidth = Math.max(2, Math.min(30, slot * 0.62))
  const labelStep = Math.max(1, Math.ceil(points.length / 6))

  const yOf = (value: number) => PAD.top + PLOT_H - (value / maxValue) * PLOT_H
  const xOf = (index: number) => PAD.left + slot * index + slot / 2
  const polyline = (value: (point: Point) => number) =>
    points.map((point, index) => `${xOf(index)},${yOf(value(point))}`).join(' ')

  const hasData = points.some((point) => valueOf(point, metric) > 0)
  const range = points.length > 0 ? `${points[0].date} 至 ${points[points.length - 1].date}` : '暂无数据'
  const ariaLabel = `${range} · ${METRIC_LABEL[metric]}趋势图（${style === 'bar' ? '柱状' : '折线'}）；每个数据点的数值见下方每日数据`
  const legend: Array<{ key: string; label: string; className: string }> = metric === 'tokens'
    ? LEGEND_TOKENS
    : [{
        key: metric,
        label: metric === 'cost' ? '估算费用' : '调用与操作',
        className: metric === 'cost' ? 'usage-chart-cost' : 'usage-chart-calls',
      }]

  const description = metric === 'cost'
    ? '按记录日期展示估算费用（美元），只统计能找到单价的记录。'
    : metric === 'calls'
      ? '按记录日期展示调用与操作次数，含没有留下 Token 的调用。'
      : '按记录日期展示生成 / 视觉与向量的 Token 量。'

  return (
    <section className="usage-panel" aria-labelledby="usage-trend-title">
      <div className="usage-panel-head">
        <div>
          <h2 id="usage-trend-title">用量趋势</h2>
          <p>{description}</p>
        </div>
        <div className="usage-controls">
          <div className="usage-segment" role="group" aria-label="趋势指标">
            {METRICS.map((option) => (
              <button
                type="button"
                key={option.key}
                className={metric === option.key ? 'selected' : ''}
                aria-pressed={metric === option.key}
                onClick={() => setMetric(option.key)}
              >
                {option.label}
              </button>
            ))}
          </div>
          <div className="usage-segment" role="group" aria-label="趋势样式">
            {STYLES.map((option) => (
              <button
                type="button"
                key={option.key}
                className={style === option.key ? 'selected' : ''}
                aria-pressed={style === option.key}
                onClick={() => setStyle(option.key)}
              >
                {option.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="usage-chart-head">
        <div className="usage-legend" aria-label="图例">
          {legend.map((item) => (
            <span key={item.key}>
              <i className={`usage-key ${item.className}`} />
              {item.label}
            </span>
          ))}
        </div>
        {all ? (
          <span className="usage-chart-hint">每根柱子代表一个有记录的日期，间距不代表实际天数；最多显示最近 30 个日期。</span>
        ) : null}
      </div>

      {points.length > 0 && hasData ? (
        <>
          <div className="usage-chart-wrap">
            <svg className="usage-chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={ariaLabel}>
              {[0, 0.5, 1].map((ratio) => {
                const y = PAD.top + PLOT_H - ratio * PLOT_H
                return (
                  <line
                    key={ratio}
                    className="usage-chart-grid"
                    x1={PAD.left}
                    x2={PAD.left + PLOT_W}
                    y1={y}
                    y2={y}
                  />
                )
              })}

              {style === 'bar'
                ? points.map((point, index) => {
                    const left = PAD.left + slot * index + (slot - barWidth) / 2
                    if (metric === 'tokens') {
                      let stacked = 0
                      return (
                        <g key={point.date} className="usage-chart-point">
                          <title>{describe(point, metric, currency)}</title>
                          {STACK.map((series) => {
                            const value = point[series.field]
                            const height = (value / maxValue) * PLOT_H
                            const y = PAD.top + PLOT_H - ((stacked + value) / maxValue) * PLOT_H
                            stacked += value
                            return (
                              <rect
                                key={series.key}
                                className={`usage-chart-bar ${series.className}`}
                                x={left}
                                y={y}
                                width={barWidth}
                                height={Math.max(0, height)}
                                rx={2}
                              />
                            )
                          })}
                        </g>
                      )
                    }
                    const height = (valueOf(point, metric) / maxValue) * PLOT_H
                    return (
                      <g key={point.date} className="usage-chart-point">
                        <title>{describe(point, metric, currency)}</title>
                        <rect
                          className={`usage-chart-bar ${metric === 'cost' ? 'usage-chart-cost' : 'usage-chart-calls'}`}
                          x={left}
                          y={PAD.top + PLOT_H - height}
                          width={barWidth}
                          height={Math.max(0, height)}
                          rx={2}
                        />
                      </g>
                    )
                  })
                : (
                  <>
                    {metric === 'tokens' ? STACK.map((series) => (
                      <polyline
                        key={series.key}
                        className={`usage-chart-line ${series.className}`}
                        points={polyline((point) => point[series.field])}
                      />
                    )) : (
                      <polyline
                        className={`usage-chart-line ${metric === 'cost' ? 'usage-chart-cost' : 'usage-chart-calls'}`}
                        points={polyline((point) => valueOf(point, metric))}
                      />
                    )}
                    {/* 折线本身在只有一天数据时画不出来，所以每条线都加可见圆点；
                        tooltip 仍挂在透明热区上，Token 指标锚在堆叠顶端（合计值）。 */}
                    {metric === 'tokens' ? STACK.map((series) => (
                      <g key={`dot-${series.key}`}>
                        {points.map((point, index) => (
                          <circle
                            key={point.date}
                            className={`usage-chart-dot ${series.className}`}
                            cx={xOf(index)}
                            cy={yOf(point[series.field])}
                            r={2.2}
                          />
                        ))}
                      </g>
                    )) : points.map((point, index) => (
                      <circle
                        key={`dot-${point.date}`}
                        className={`usage-chart-dot ${metric === 'cost' ? 'usage-chart-cost' : 'usage-chart-calls'}`}
                        cx={xOf(index)}
                        cy={yOf(valueOf(point, metric))}
                        r={2.5}
                      />
                    ))}
                    {points.map((point, index) => (
                      <circle
                        key={point.date}
                        className="usage-chart-hit"
                        cx={xOf(index)}
                        cy={yOf(valueOf(point, metric))}
                        r={7}
                      >
                        <title>{describe(point, metric, currency)}</title>
                      </circle>
                    ))}
                  </>
                )}

              {points.map((point, index) => (
                index % labelStep === 0 || index === points.length - 1
                  ? (
                      <text
                        key={point.date}
                        className="usage-chart-axis"
                        x={xOf(index)}
                        y={H - 8}
                        textAnchor="middle"
                      >
                        {point.date.slice(5)}
                      </text>
                    )
                  : null
              ))}
            </svg>
          </div>
          {all && <div className="usage-range"><span>{points[0].date}</span>{points.length > 1 && <span>{points[points.length - 1].date}</span>}</div>}
        </>
      ) : (
        <div className="usage-empty">{emptyText(metric, all)}</div>
      )}

      {points.length > 0 && (
        <details className="usage-daily">
          <summary>查看每日数据（UTC）</summary>
          <div className="table-wrap table-wrap--scroll usage-daily-table">
            <table className="table usage-numeric">
              <thead>
                <tr>
                  <th>日期</th>
                  <th>调用与操作</th>
                  <th>输入</th>
                  <th>输出</th>
                  <th>向量</th>
                  <th>Token 合计</th>
                  <th>估算费用</th>
                </tr>
              </thead>
              <tbody>
                {data.daily.slice().reverse().map((point) => (
                  <tr key={point.date}>
                    <td className="strong">{point.date}</td>
                    <td>{formatNumber(point.calls)}</td>
                    <td>{formatNumber(point.prompt_tokens)}</td>
                    <td>{formatNumber(point.completion_tokens)}</td>
                    <td>{formatNumber(point.embed_tokens)}</td>
                    <td>{formatNumber(point.total_tokens)}</td>
                    <td>{formatAmount(point.cost.amount, currency)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </section>
  )
}

import { useCallback, useEffect, useState } from 'react'
import { usageApi, type UsageSummary } from '../api'
import { Button, ErrorState, LoadingBlock } from '../components/ui'
import { errorText, usePolling } from '../utils'
import UsageBreakdown from './usage/UsageBreakdown'
import UsageMetrics from './usage/UsageMetrics'
import UsageTrend from './usage/UsageTrend'
import PricingEditor from './usage/PricingEditor'
import './UsagePage.css'

type Period = 7 | 30 | 0

const PERIODS: Array<{ days: Period; label: string }> = [
  { days: 7, label: '近 7 天' },
  { days: 30, label: '近 30 天' },
  { days: 0, label: '全部' },
]

const AUTO_REFRESH_MS = 60_000

/**
 * 管理员用量页。
 *
 * 后端 `/api/usage` 只接受 0/7/30 三档，所以时间范围就只有这三个按钮；
 * 页面本身不推算任何接口没给的数字（延迟、缓存、失败次数等），只在页脚点名缺失。
 * 自动刷新默认关闭：这是低频查看的页面，不该在后台一直打数据库。
 */
export default function UsagePage() {
  const [period, setPeriod] = useState<Period>(30)
  const [autoRefresh, setAutoRefresh] = useState(false)
  const [pricingOpen, setPricingOpen] = useState(false)
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

  const refresh = useCallback(() => setRevision((value) => value + 1), [])

  usePolling(refresh, AUTO_REFRESH_MS, autoRefresh)

  // 刷新时保留上一次的数据：自动刷新每 60 秒一次，整块换成加载态会让页面一直闪。
  const showLoader = loading && !data
  const showError = error !== null && !data

  return (
    <div className="usage-page">
      <div className="page-head usage-heading">
        <div>
          <h1>用量概览</h1>
          <p className="sub">本应用记录的知识库处理、问答与个人 Agent 执行；金额是按参考单价估算出来的值。</p>
        </div>
        <div className="spacer" />
        <div className="page-head-actions usage-actions">
          <div className="usage-segment" role="group" aria-label="选择统计时间范围">
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
          <label className="usage-auto">
            <input
              type="checkbox"
              className="checkbox"
              aria-label="每 60 秒自动刷新"
              checked={autoRefresh}
              onChange={(event) => setAutoRefresh(event.target.checked)}
            />
            每 60 秒自动刷新
          </label>
          <Button variant="ghost" onClick={refresh} loading={loading && Boolean(data)}>
            刷新
          </Button>
        </div>
      </div>

      <div className="usage-toolbar">
        <span className="usage-timezone">按 UTC 日期统计</span>
        {loading && data ? <span className="usage-refreshing" role="status">正在刷新…</span> : null}
      </div>

      {error !== null && data ? (
        <p className="usage-refresh-error" role="alert">
          刷新失败：{errorText(error)}
          <button type="button" className="btn-link" onClick={refresh}>重试</button>
        </p>
      ) : null}

      {showLoader ? (
        <div className="card"><LoadingBlock text="正在加载用量记录…" /></div>
      ) : showError ? (
        <div className="card"><ErrorState error={error} onRetry={refresh} /></div>
      ) : data ? (
        <>
          <UsageMetrics
            totals={data.totals}
            currency={data.currency}
            onManagePricing={() => setPricingOpen(true)}
          />
          <UsageTrend data={data} />
          <UsageBreakdown data={data} />

          <footer className="usage-footer">
            <div>
              <strong>统计范围</strong>
              <p>
                只汇总本应用保留的知识库处理流水、成功保存的问答，以及状态为成功的个人 Agent
                执行；检索调试、连接测试、失败或中断的调用不在其中。日期按 UTC 统计，离线操作不会产生供应商费用。
              </p>
            </div>
            <div>
              <strong>费用口径</strong>
              <p>
                金额 = 输入 Token × 输入单价 + 输出 Token × 输出单价，按每 100 万 token
                的美元参考价估算，只对能找到单价的记录计算；向量 Token 一律不参与金额。这是估算值，不是账单、余额或实际支出。
              </p>
            </div>
            <div>
              <strong>未收录的维度</strong>
              <p>
                接口没有返回、这里也不推算：缓存命中、首字延迟与总耗时、生成速度、失败与重试次数、供应商与账号拆分、逐条调用明细、人民币换算。
              </p>
              <p>
                供应商可能不返回 Token 数，失败或中断的调用也可能消耗用量，所以这些数字不能当作完整用量。
              </p>
            </div>
          </footer>
        </>
      ) : null}

      <PricingEditor
        open={pricingOpen}
        onClose={() => setPricingOpen(false)}
        onChanged={refresh}
      />
    </div>
  )
}

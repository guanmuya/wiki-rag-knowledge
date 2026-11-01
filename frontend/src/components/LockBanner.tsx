import { useEffect, useState } from 'react'
import { BACKEND_BUSY_CLEARED_EVENT, BACKEND_BUSY_EVENT, isDesktop } from '../api'

/**
 * 全局「后端正在写入」横幅。
 *
 * 为什么要有它：SQLite 写锁期间，用户点的任何一个操作（上传、重试、切模型）
 * 都会失败，但失败点分散在各个页面，用户看不到「这是同一个原因」。
 * 这里在 api 层统一检测（503 / `database is locked`）后广播一次事件，
 * 由这一条横幅说明全局状态，各页面只负责保留自己的数据。
 *
 * 放在 <main> 顶部而不是浮层：不遮挡内容、不改变现有布局结构，
 * 出现/消失时只是把页面往下推一行。
 */
export default function LockBanner() {
  const [visible, setVisible] = useState(false)
  const [since, setSince] = useState(0)

  useEffect(() => {
    const onBusy = () => {
      setVisible(true)
      // 用时间戳而不是布尔值：锁持续期间会反复收到事件，
      // 靠它把「自动收起」的计时重新推后，横幅不会在还卡着的时候自己消失。
      setSince(Date.now())
    }
    const onCleared = () => setVisible(false)
    window.addEventListener(BACKEND_BUSY_EVENT, onBusy)
    window.addEventListener(BACKEND_BUSY_CLEARED_EVENT, onCleared)
    return () => {
      window.removeEventListener(BACKEND_BUSY_EVENT, onBusy)
      window.removeEventListener(BACKEND_BUSY_CLEARED_EVENT, onCleared)
    }
  }, [])

  useEffect(() => {
    if (!visible) return
    // 兜底：万一后续请求都不再发生（用户停在静态页面上），横幅也不会一直挂着
    const timer = window.setTimeout(() => setVisible(false), 15_000)
    return () => window.clearTimeout(timer)
  }, [visible, since])

  if (!visible) return null

  return (
    <div className="lock-banner" role="status" aria-live="polite">
      <span className="lock-banner-icon">⏳</span>
      <div className="lock-banner-body">
        <strong>后台正在写入，稍后自动恢复</strong>
        <div className="lock-banner-detail">
          数据库正被其它任务占用（例如正在向量化文档）。列表会保留上次结果，可以继续浏览其它页面，稍后刷新。
        </div>
      </div>
      <div className="lock-banner-actions">
        <button type="button" className="btn-link" onClick={() => window.location.reload()}>
          刷新页面
        </button>
        {isDesktop() ? (
          <button
            type="button"
            className="btn-link"
            onClick={() => void window.wrkDesktop?.openPath('logs')}
          >
            打开日志
          </button>
        ) : null}
        <button type="button" className="btn-link" onClick={() => setVisible(false)}>
          知道了
        </button>
      </div>
    </div>
  )
}

import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { metaApi } from '../api'
import { useAuth } from '../auth'
import type { MetaInfo } from '../types'

const DISMISS_KEY = 'wrk_offline_banner_dismissed'

/**
 * 离线演示模式提示条。
 * 后端 /api/meta 的 offline_models=true 表示没有可用的对话/向量模型，
 * 此时问答会退化为「抽取式回答」，需要在界面显式说明，避免被当成模型效果差。
 */
export default function OfflineBanner() {
  const { isAdmin } = useAuth()
  const [meta, setMeta] = useState<MetaInfo | null>(null)
  const [dismissed, setDismissed] = useState(false)

  useEffect(() => {
    try {
      setDismissed(localStorage.getItem(DISMISS_KEY) === '1')
    } catch {
      /* 隐私模式忽略 */
    }
  }, [])

  const load = useCallback(async () => {
    try {
      const data = await metaApi.get()
      setMeta(data)
    } catch {
      setMeta(null)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  if (!meta?.offline_models || dismissed) return null

  const llmOk = meta.capabilities?.llm_configured ?? false
  const embedOk = meta.capabilities?.embedding_configured ?? false

  const dismiss = () => {
    setDismissed(true)
    try {
      localStorage.setItem(DISMISS_KEY, '1')
    } catch {
      /* 忽略 */
    }
  }

  return (
    <div className="offline-banner" role="status">
      <span className="offline-banner-icon">⚡</span>
      <div className="offline-banner-body">
        <strong>当前未配置模型，运行在离线演示模式</strong>
        <div className="offline-banner-detail">
          问答会退化为「检索片段抽取」而非模型生成
          {!llmOk && !embedOk
            ? '（对话模型与向量模型均未配置）'
            : !llmOk
              ? '（对话模型未配置）'
              : '（向量模型未配置，仅关键词检索）'}
          。配置后即可获得正常生成效果。
        </div>
      </div>
      <div className="offline-banner-actions">
        {isAdmin ? (
          <Link className="btn btn-sm" to="/admin/models">
            去配置模型
          </Link>
        ) : (
          <span className="tiny muted">请联系管理员配置</span>
        )}
        <button type="button" className="btn-link" onClick={dismiss}>
          不再提示
        </button>
      </div>
    </div>
  )
}

/** 供「关于页」等地方复用：判断提示条是否已被关闭（用于重置）。 */
export function resetOfflineBannerDismiss(): void {
  try {
    localStorage.removeItem(DISMISS_KEY)
  } catch {
    /* 忽略 */
  }
}

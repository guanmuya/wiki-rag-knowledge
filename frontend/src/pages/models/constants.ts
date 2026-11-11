import type { DiscoveredModel, ModelPurpose, Provider, ProviderProtocol } from '../../types'
import { IconEye, IconLayers, IconMessage } from '../../components/icons'

export const PURPOSE_LABEL: Record<ModelPurpose, string> = {
  llm: '对话模型',
  embedding: '向量模型',
  vision: '视觉模型',
}

/** 三个用途各配一枚线性图标，颜色跟随文本角色，不靠彩色徽标区分。 */
export const PURPOSE_ICON: Record<ModelPurpose, typeof IconMessage> = {
  llm: IconMessage,
  embedding: IconLayers,
  vision: IconEye,
}

export const PURPOSE_ORDER: ModelPurpose[] = ['llm', 'embedding', 'vision']

/** 折叠状态只影响本地展示，存 localStorage，刷新或再次进入页面后保持。 */
export const COLLAPSED_GROUPS_KEY = 'wrk_model_groups_collapsed'

export function readCollapsedGroups(): Record<ModelPurpose, boolean> {
  const collapsed: Record<ModelPurpose, boolean> = { llm: false, embedding: false, vision: false }
  try {
    const raw = localStorage.getItem(COLLAPSED_GROUPS_KEY)
    if (!raw) return collapsed
    const saved: unknown = JSON.parse(raw)
    if (Array.isArray(saved)) {
      for (const purpose of PURPOSE_ORDER) {
        if (saved.some((item) => item === purpose)) collapsed[purpose] = true
      }
    }
  } catch {
    /* 隐私模式或本地数据损坏：按全部展开处理 */
  }
  return collapsed
}

/** 供应商协议中文短名，表格里用。 */
export const PROTOCOL_SHORT: Record<ProviderProtocol, string> = {
  openai: 'OpenAI 兼容',
  'openai-responses': 'Responses',
  anthropic: 'Anthropic',
}

export const PROTOCOL_HINT: Record<ProviderProtocol, string> = {
  openai: '适用于 OpenAI 和兼容协议的直连 API；支持的接口因供应商而异。',
  'openai-responses': '适用于支持 /responses 的直连 API；不支持 Embedding。',
  anthropic: 'Anthropic 直连 API 使用 /v1/messages；不支持 Embedding。',
}

export const PROVIDER_PRESETS: Array<{ name: string; baseUrl: string; protocol: ProviderProtocol }> = [
  { name: 'OpenAI', baseUrl: 'https://api.openai.com/v1', protocol: 'openai' },
  { name: 'Anthropic', baseUrl: 'https://api.anthropic.com', protocol: 'anthropic' },
  { name: 'DeepSeek', baseUrl: 'https://api.deepseek.com/v1', protocol: 'openai' },
  { name: 'Kimi（中国区）', baseUrl: 'https://api.moonshot.cn/v1', protocol: 'openai' },
  { name: 'Kimi（国际区）', baseUrl: 'https://api.moonshot.ai/v1', protocol: 'openai' },
  { name: 'Qwen（中国区）', baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', protocol: 'openai' },
  { name: 'Google Gemini', baseUrl: 'https://generativelanguage.googleapis.com/v1beta/openai', protocol: 'openai' },
]

export function normalizedProviderBase(value: string): string {
  const trimmed = value.trim()
  try {
    const url = new URL(trimmed)
    url.pathname = url.pathname.replace(/\/+$/, '') || '/'
    return url.href
  } catch {
    return trimmed.replace(/\/+$/, '')
  }
}

export function protocolOf(p: Provider): ProviderProtocol {
  // 老库回填/异常值统一按 openai 处理，与后端 provider_protocol() 一致
  const value = p.protocol
  if (value === 'openai-responses' || value === 'anthropic' || value === 'openai') return value
  return 'openai'
}

/** 探测结果 + 用户勾选状态，导入弹窗用。 */
export interface DiscoverRow extends DiscoveredModel {
  checked: boolean
  purpose: ModelPurpose
}

/** 知识库向量签名一致性的四种取值：页面算、卡片展示，共用这一处定义。 */
export type EmbedKbState = 'unindexed' | 'unknown' | 'consistent' | 'mismatch'

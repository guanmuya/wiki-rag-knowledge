/**
 * 用量页的金额、单价与「这个价从哪来」文案。
 *
 * 口径只在这里写一次：金额一律「估算」，只有美元，单价一律「每 100 万 token」。
 * 页面任何地方都不出现「账单」「余额」「实际支出」——后端返回的 amount 是
 * 按参考单价算出来的估算值（见 docs/API.md §7.2），换个措辞会让它看起来像账单。
 */
import type { EffectiveModelPrice } from '../../api'
import type { BadgeTone } from '../../utils'

/** 金额：USD 走 `$`，其它币种保留「数字 币种」，不做汇率换算。 */
export function formatAmount(amount: number, currency: string): string {
  const value = Number.isFinite(amount) ? amount : 0
  // 估算值可能小到 1e-6 级别（几条向量/短问答），两位小数会全变成 $0.00，
  // 所以小数位给它 6 位；整数与常见金额仍然按两位显示。
  const text = value.toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 6,
  })
  return currency === 'USD' ? `$${text}` : `${text} ${currency}`
}

/** 单价：`$2.00 / 百万 token`；`null` 表示这个模型没有价（未计价，不是 0 元）。 */
export function formatPrice(per1m: number | null, currency: string): string {
  if (per1m === null || !Number.isFinite(per1m)) return '未填单价'
  const text = per1m.toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  })
  return `${currency === 'USD' ? `$${text}` : `${text} ${currency}`} / 百万 token`
}

/** 生效价来源：`user` 自己填的、`builtin` 内置参考价、`none` 未计价。 */
export const ORIGIN_LABEL: Record<EffectiveModelPrice['origin'], string> = {
  user: '自己填的价',
  builtin: '内置参考价',
  none: '未计价',
}

export const ORIGIN_TONE: Record<EffectiveModelPrice['origin'], BadgeTone> = {
  user: 'info',
  builtin: 'muted',
  none: 'warn',
}

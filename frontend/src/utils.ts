import { useEffect, useRef } from 'react'

/**
 * 解析后端时间戳。
 *
 * 后端历史输出过**不带时区**的 UTC 字符串（如 `2026-11-06T12:45:09.883980`）。
 * 按 ECMAScript 规范，这种字符串会被当作本地时间解析，于是东八区用户看到的
 * 每个时间都差 8 小时，任务心跳还会一创建就显示「已运行 8 小时」。
 * 后端已改为输出带偏移的 UTC，这里再兜一层：没有时区标识的时间按 UTC 处理。
 */
export function parseApiTime(value: string | null | undefined): Date | null {
  if (!value) return null
  const text = value.trim()
  if (!text) return null
  const hasClock = /\d{1,2}:\d{2}/.test(text)
  const hasZone =
    /([zZ]|[+-]\d{2}:?\d{2})$/.test(text) || (hasClock && /[+-]\d{2}$/.test(text))
  if (!hasClock) {
    // 纯日期（如用量统计的 `2026-11-06`）没有时间部分，按本地日历日解析，
    // 不要拼 Z：`new Date("2026-11-06")` 在 UTC 负偏移时区会退到前一天。
    const d = new Date(`${text}T00:00:00`)
    return Number.isNaN(d.getTime()) ? null : d
  }
  const d = new Date(hasZone ? text : `${text}Z`)
  return Number.isNaN(d.getTime()) ? null : d
}

/** 时间格式化：ISO → 本地 `YYYY-MM-DD HH:mm`。 */
export function formatTime(value: string | null | undefined): string {
  const d = parseApiTime(value)
  if (!d) return '—'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(
    d.getMinutes(),
  )}`
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`
}

export function formatNumber(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '0'
  return n.toLocaleString('zh-CN')
}

/**
 * 距某个时间点过去了多少秒（null / 非法值返回 null）。
 * 任务心跳要显示「距今多少秒」，同时 >30s 无进展的判定也要用同一个数值，
 * 所以把「取秒数」和「排版」拆开，避免两处各算一遍导致口径不一致。
 */
export function elapsedSeconds(
  value: string | null | undefined,
  now: number = Date.now(),
): number | null {
  const d = parseApiTime(value)
  if (!d) return null
  return Math.max(0, Math.round((now - d.getTime()) / 1000))
}

/**
 * 相对时间：ISO → 「刚刚 / 12 秒 / 3 分 20 秒 / 1 小时 5 分」。
 * 用于任务卡片的「已运行 X」心跳。刻意不用 `Intl.RelativeTimeFormat`：
 * 它给的是「12 秒钟前」这类整句，塞进 `stage · 45% · …` 一行里读起来更绕。
 */
export function formatSince(value: string | null | undefined, now: number = Date.now()): string {
  const s = elapsedSeconds(value, now)
  if (s === null) return '—'
  if (s < 1) return '刚刚'
  if (s < 60) return `${s} 秒`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} 分 ${String(s % 60).padStart(2, '0')} 秒`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h} 小时 ${m % 60} 分`
  return `${Math.floor(h / 24)} 天 ${h % 24} 小时`
}

export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—'
  if (ms < 1000) return `${Math.round(ms)} ms`
  return `${(ms / 1000).toFixed(2)} s`
}

/** 错误对象 → 中文文案。 */
export function errorText(err: unknown): string {
  if (err instanceof Error && err.message) return err.message
  if (typeof err === 'string' && err) return err
  return '操作失败，请稍后重试'
}

/** 简易轮询 hook：enabled=false 或组件卸载时自动停止。 */
export function usePolling(
  fn: () => void | Promise<void>,
  intervalMs: number,
  enabled: boolean,
): void {
  const saved = useRef(fn)
  saved.current = fn

  useEffect(() => {
    if (!enabled) return
    let alive = true
    let timer = 0
    const tick = async () => {
      if (!alive) return
      try {
        await saved.current()
      } catch {
        /* 轮询失败静默，下一轮再试 */
      }
      if (alive) timer = window.setTimeout(tick, intervalMs)
    }
    void tick()
    return () => {
      alive = false
      window.clearTimeout(timer)
    }
  }, [enabled, intervalMs])
}

/** 任务是否仍在跑（队列中或执行中）。 */
export function isRunningStatus(status: string): boolean {
  return status === 'queued' || status === 'running'
}

export type BadgeTone = 'success' | 'warn' | 'danger' | 'partial' | 'muted' | 'info'

export function taskStatusClass(status: string): BadgeTone {
  switch (status) {
    case 'success':
    case 'ready':
    case 'published':
    case 'active':
      return 'success'
    case 'running':
    case 'indexing':
    case 'queued':
    case 'needs_review':
    case 'draft':
      return 'warn'
    case 'failed':
    case 'disabled':
      return 'danger'
    case 'partial':
      return 'partial'
    default:
      return 'muted'
  }
}

export function wikiStatusLabel(status: string, fallback?: string): string {
  if (fallback) return fallback
  switch (status) {
    case 'published':
      return '已发布'
    case 'draft':
      return '草稿'
    case 'needs_review':
      return '待校对'
    default:
      return status
  }
}

export function kbStatusLabel(status: string, fallback?: string): string {
  if (fallback) return fallback
  switch (status) {
    case 'empty':
      return '空库'
    case 'indexing':
      return '索引中'
    case 'ready':
      return '可用'
    case 'partial':
      return '部分可用'
    case 'failed':
      return '失败'
    default:
      return status
  }
}

export function modeLabel(mode: string, fallback?: string): string {
  if (fallback) return fallback
  return mode === 'wiki' ? 'Wiki 增强' : '基础分块'
}

export function routeUsedLabel(used: string): string {
  switch (used) {
    case 'wiki':
      return 'Wiki'
    case 'chunk':
      return '分块'
    case 'none':
      return '未命中'
    default:
      return used
  }
}

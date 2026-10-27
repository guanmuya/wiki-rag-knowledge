import { useEffect, useRef, useState } from 'react'

/** 时间格式化：ISO → 本地 `YYYY-MM-DD HH:mm`。 */
export function formatTime(value: string | null | undefined): string {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return '—'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(
    d.getMinutes(),
  )}`
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return '—'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
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

/** 受控输入防抖值。 */
export function useDebounced<T>(value: T, delay = 300): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(value), delay)
    return () => window.clearTimeout(t)
  }, [value, delay])
  return debounced
}

export const TASK_RUNNING_STATUSES = ['queued', 'running'] as const

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

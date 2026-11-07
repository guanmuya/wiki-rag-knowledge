import { useEffect, useRef, useState } from 'react'
import type { Task } from '../types'
import {
  errorText,
  formatSince,
  formatTime,
  isRunningStatus,
  taskStatusClass,
} from '../utils'
import { Badge, EmptyState, ProgressBar, Spinner } from './ui'
import { IconAlert, IconClock } from './icons'

/**
 * 「无进展」阈值：运行中任务的 stage/progress 超过这个时间都没变，
 * 就认为它可能卡在等写锁上。选 30s 是因为正常分块/向量化每批都会推进进度，
 * 半分钟不动已经明显不正常，但还不至于让用户以为任务失败了。
 */
const STALE_AFTER_MS = 30_000

function wikiStopRunKey(task: Task): string {
  return JSON.stringify([task.id, task.run_token])
}

interface TaskListProps {
  tasks: Task[]
  loading?: boolean
  error?: unknown
  onRetry?: (task: Task) => Promise<void> | void
  onRetryError?: (err: unknown) => void
  onStopWiki?: (task: Task) => Promise<void>
  onStopWikiError?: (err: unknown) => void
  /** 任务中心混合多个知识库，需要逐条判断管理权限。 */
  canManageTask?: (task: Task) => boolean
  showKb?: boolean
  emptyText?: string
}

export default function TaskList({
  tasks,
  loading,
  error,
  onRetry,
  onRetryError,
  onStopWiki,
  onStopWikiError,
  canManageTask,
  showKb = false,
  emptyText = '暂无任务',
}: TaskListProps) {
  const [retrying, setRetrying] = useState<number | null>(null)
  const [stopping, setStopping] = useState<Set<string>>(() => new Set())
  // 已受理但刷新尚未确认时保留禁用状态；任务重试后不沿用上次的状态。
  const [accepted, setAccepted] = useState<Set<string>>(() => new Set())
  const stoppingRef = useRef(new Set<string>())
  // hooks 必须在任何 return 之前调用：心跳秒数与「无进展」判定都依赖它们
  const now = useHeartbeatClock(tasks)
  const stale = useStaleTasks(tasks, now)

  useEffect(() => {
    const activeRuns = new Set(
      tasks.filter((task) => isRunningStatus(task.status) && !task.wiki_stop_requested)
        .map(wikiStopRunKey),
    )
    setAccepted((previous) => {
      const next = new Set([...previous].filter((key) => activeRuns.has(key)))
      return next.size === previous.size ? previous : next
    })
  }, [tasks, accepted])

  const hasData = tasks.length > 0

  if (loading && !hasData) {
    return (
      <div className="state-block inline">
        <Spinner size={16} />
        <span>任务加载中…</span>
      </div>
    )
  }

  // 只有「一条都没拿到」时才整块报错。轮询失败但手里有上次数据时，
  // 下面走顶部提示条——清空列表会让用户以为任务被删了。
  if (error && !hasData) {
    return (
      <div className="state-block inline" style={{ color: 'var(--c-danger)' }}>
        <span>任务加载失败：{errorText(error)}</span>
      </div>
    )
  }

  if (!hasData) {
    return <EmptyState title={emptyText} hint="上传文档或重建索引后，任务会显示在这里。" />
  }

  const handleRetry = async (task: Task) => {
    if (!onRetry) return
    setRetrying(task.id)
    try {
      await onRetry(task)
    } catch (err) {
      onRetryError?.(err)
    } finally {
      setRetrying(null)
    }
  }

  const handleStopWiki = async (task: Task) => {
    const runKey = wikiStopRunKey(task)
    if (!onStopWiki || !task.can_stop_wiki || stoppingRef.current.has(runKey)) return
    stoppingRef.current.add(runKey)
    setStopping((previous) => new Set(previous).add(runKey))
    try {
      await onStopWiki(task)
      setAccepted((previous) => new Set(previous).add(runKey))
    } catch (err) {
      onStopWikiError?.(err)
    } finally {
      stoppingRef.current.delete(runKey)
      setStopping((previous) => {
        const next = new Set(previous)
        next.delete(runKey)
        return next
      })
    }
  }

  return (
    <div className="task-list">
      {error ? (
        <div className="poll-notice" role="status">
          <span className="poll-notice-icon"><IconAlert size={14} /></span>
          <div className="poll-notice-body">
            <strong>本次刷新失败，下方仍是上次获取到的任务</strong>
            <div className="poll-notice-detail">
              {errorText(error)}（每 2 秒会自动重试，恢复后提示条自动消失）
            </div>
          </div>
        </div>
      ) : null}

      {tasks.map((task) => {
        const running = isRunningStatus(task.status)
        const mayManage = canManageTask?.(task) ?? true
        const runKey = wikiStopRunKey(task)
        const stopPending = stopping.has(runKey) || accepted.has(runKey) || Boolean(task.wiki_stop_requested)
        // 心跳优先取 updated_at（后端补上后就是真实的「最后进展」时间），
        // 现在后端 Task 还没这个字段，退回 started_at / created_at。
        const beatAt = task.updated_at ?? task.started_at ?? task.created_at
        const heartbeat = formatSince(beatAt, now)
        const heartbeatLabel = task.updated_at ? '最近更新' : '已运行'
        const isStale = stale.has(task.id)
        const wikiRebuild = task.kind === 'rebuild_wiki'
        return (
          <div className="task-item" key={task.id}>
            <div className="task-head">
              <span className="task-kind">{task.kind_label || task.kind}</span>
              <span role={wikiRebuild ? 'status' : undefined}>
                <Badge tone={taskStatusClass(task.status)}>
                  {task.status === 'stopped' ? '已停止' : task.status_label || statusText(task.status)}
                </Badge>
              </span>
              {showKb && task.kb_name ? (
                <span className="tiny muted">· {task.kb_name}</span>
              ) : null}
              {task.doc_name ? <span className="task-subject">· {task.doc_name}</span> : null}
              <span className="spacer" />
              {/* 排队中的任务用 created_at 当心跳：它还没有 started_at */}
              {task.status === 'queued' ? (
                <span className="tiny muted nowrap">已排队 {heartbeat}</span>
              ) : (
                <span className="tiny muted nowrap">{formatTime(task.created_at)}</span>
              )}
              {onStopWiki && mayManage && running && (task.can_stop_wiki || stopPending) ? (
                <button
                  type="button"
                  className="btn-link"
                  disabled={stopPending || !task.can_stop_wiki}
                  title="只停止这次 Wiki 生成，其他任务仍可能触发生成"
                  onClick={() => void handleStopWiki(task)}
                >
                  {stopPending ? '正在停止…' : '停止生成'}
                </button>
              ) : null}
              {(task.status === 'failed' || task.status === 'partial' || (task.status === 'stopped' && wikiRebuild)) && onRetry && mayManage ? (
                <button
                  type="button"
                  className="btn-link"
                  disabled={retrying === task.id}
                  title={task.status === 'stopped' ? '重新执行整个任务，可能再次生成 Wiki' : undefined}
                  onClick={() => void handleRetry(task)}
                >
                  {retrying === task.id ? '重试中…' : '重试'}
                </button>
              ) : null}
            </div>

            {running ? (
              <div className="row" style={{ gap: 10 }}>
                {wikiRebuild ? (
                  task.status === 'running' ? <Spinner size={14} /> : null
                ) : (
                  <div style={{ flex: 1, maxWidth: 320 }}>
                    <ProgressBar value={task.progress} />
                  </div>
                )}
                <span className="task-stage">
                  <span role={wikiRebuild ? 'status' : undefined}>{task.stage || '处理中'}</span>
                  {wikiRebuild ? null : ` · ${task.progress}%`}
                  {/* 排队中的任务还没开始跑：等待时长在行首右侧已经显示过一次，
                      这里不再重复「已排队」，也不该说「已运行」。 */}
                  {task.status === 'queued' ? null : (
                    <span className="task-heartbeat">
                      {' '}
                      · {heartbeatLabel} {heartbeat}
                    </span>
                  )}
                </span>
              </div>
            ) : null}

            {/*
              无进展提示刻意做成浅色说明条而不是红色报错：任务还在后台跑，
              只是被别的写事务挡住了，显示成失败会让用户去点删除或反复重启应用。
            */}
            {running && (task.wiki_stop_requested || accepted.has(runKey)) ? (
              <div className="task-hint" role="status">
                当前步骤完成后停止本次 Wiki 生成；已入库的分块保留，其他任务仍可能生成 Wiki。
              </div>
            ) : isStale ? (
              <div className="task-hint" role="status">
                <span className="task-hint-icon"><IconClock size={13} /></span>
                <span>{staleHintText(task.status, task.kind)}</span>
              </div>
            ) : null}

            {task.status === 'stopped' ? (
              <div className="tiny muted">
                {wikiRebuild
                  ? '本次 Wiki 生成已停止。重试任务、后续文档入库或删除、重建索引及手动重建 Wiki 可能再次生成。'
                  : '本次 Wiki 生成已停止。可在知识库设置中重建 Wiki；后续文档入库或删除、重建索引也可能再次生成。'}
              </div>
            ) : null}

            {(task.status === 'success' || task.status === 'stopped') && task.result && Object.keys(task.result).length > 0 ? (
              <div className="tiny muted">{resultText(task.result)}</div>
            ) : null}

            {task.error ? (
              <div className="task-error">{task.error}</div>
            ) : task.last_error && running ? (
              <div className="tiny muted">上次失败：{task.last_error}</div>
            ) : null}

            {task.attempts > 1 || (task.retry_count ?? 0) > 0 ? (
              <div className="tiny muted">
                {[
                  task.attempts > 1 ? `已执行 ${task.attempts} 次` : '',
                  (task.retry_count ?? 0) > 0 ? `手动重试 ${task.retry_count} 次` : '',
                ]
                  .filter(Boolean)
                  .join(' · ')}
              </div>
            ) : null}
          </div>
        )
      })}
    </div>
  )
}

/**
 * 每秒走一次的时钟，只在有运行中任务时启动。
 * 轮询是 2 秒一次，如果心跳秒数也跟着 2 秒跳，「已运行 12 秒 → 14 秒」看起来
 * 反而更像卡住了；这里单独 1s tick，轮询仍负责取数据。
 */
function useHeartbeatClock(tasks: Task[]): number {
  const running = tasks.some((t) => isRunningStatus(t.status))
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!running) return
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [running])

  return now
}

/**
 * 找出「运行中但长时间没有任何进展」的任务。
 *
 * 为什么用本地观测而不是只看 started_at：一个正常的大文档向量化可能跑几分钟，
 * 只看「开始多久」会把所有慢任务都标成可疑。这里比较 (status, stage, progress)
 * 这组签名，签名变了就重置计时——只有真的不动才提示。
 */
function useStaleTasks(tasks: Task[], now: number): Set<number> {
  const watch = useRef(new Map<number, { sig: string; at: number }>())
  const stale = new Set<number>()
  const alive = new Set<number>()

  tasks.forEach((task) => {
    alive.add(task.id)
    const sig = `${task.status}|${task.stage}|${task.progress}`
    const prev = watch.current.get(task.id)
    if (!prev || prev.sig !== sig) {
      watch.current.set(task.id, { sig, at: Date.now() })
      return
    }
    if (isRunningStatus(task.status) && now - prev.at >= STALE_AFTER_MS) stale.add(task.id)
  })

  // 被筛掉或删除的任务不再跟踪，避免这个 Map 随历史记录一直增长
  Array.from(watch.current.keys()).forEach((id) => {
    if (!alive.has(id)) watch.current.delete(id)
  })

  return stale
}

/**
 * 「卡住」时的说明文案。
 * 排队中与执行中是两种不同的等待：前者在等轮到自己，后者在等写锁，
 * 说成同一句会让用户以为「排队也在写数据库」。
 */
function staleHintText(status: string, kind: string): string {
  if (status === 'queued') {
    return '排队超过 30 秒仍未开始，可能在等待其它任务写入，可稍后再看（不是失败）。'
  }
  if (kind === 'rebuild_wiki') {
    return '当前阶段尚未完成，Wiki 生成可能需要较长时间，请稍后查看。'
  }
  return '超过 30 秒没有新进展，可能在等待其它任务写入，可点重试或稍后再看（任务仍在后台运行，不是失败）。'
}

function statusText(status: string): string {
  switch (status) {
    case 'queued':
      return '排队中'
    case 'running':
      return '处理中'
    case 'success':
      return '成功'
    case 'partial':
      return '部分失败'
    case 'failed':
      return '失败'
    case 'stopped':
      return '已停止'
    default:
      return status
  }
}

function resultText(result: Record<string, unknown>): string {
  const parts: string[] = []
  const map: Record<string, string> = {
    chunks: '分块',
    pages: '页面',
    documents: '文档',
    kb_count: '知识库',
    vectors: '向量',
    chars: '字符',
    images: '图片',
    embedding_mode: '向量模式',
    embedding_signature: '向量签名',
    wiki_error: 'Wiki 失败原因',
    created: '新建页面',
    updated: '更新页面',
    skipped: '跳过页面',
    sections: '章节',
  }
  Object.entries(result).forEach(([k, v]) => {
    if (typeof v === 'number' || typeof v === 'string') {
      // 后端的 embedding_mode 只有 live / keyword-only 两种：live 表示「这次入库
      // 产出了向量」（离线哈希向量也算），并不代表配置了在线向量模型，不能标成「在线模型」。
      const text = k === 'embedding_mode' ? (v === 'live' ? '已向量化' : '仅关键词') : v
      parts.push(`${map[k] ?? k}: ${text}`)
    }
  })
  return parts.join(' · ')
}

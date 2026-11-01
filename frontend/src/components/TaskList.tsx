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

/**
 * 「无进展」阈值：运行中任务的 stage/progress 超过这个时间都没变，
 * 就认为它可能卡在等写锁上。选 30s 是因为正常分块/向量化每批都会推进进度，
 * 半分钟不动已经明显不正常，但还不至于让用户以为任务失败了。
 */
const STALE_AFTER_MS = 30_000

interface TaskListProps {
  tasks: Task[]
  loading?: boolean
  error?: unknown
  onRetry?: (task: Task) => Promise<void> | void
  onRetryError?: (err: unknown) => void
  showKb?: boolean
  emptyText?: string
}

export default function TaskList({
  tasks,
  loading,
  error,
  onRetry,
  onRetryError,
  showKb = false,
  emptyText = '暂无任务',
}: TaskListProps) {
  const [retrying, setRetrying] = useState<number | null>(null)
  // hooks 必须在任何 return 之前调用：心跳秒数与「无进展」判定都依赖它们
  const now = useHeartbeatClock(tasks)
  const stale = useStaleTasks(tasks, now)

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

  return (
    <div className="task-list">
      {error ? (
        <div className="poll-notice" role="status">
          <span className="poll-notice-icon">⚠</span>
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
        // 心跳优先取 updated_at（后端补上后就是真实的「最后进展」时间），
        // 现在后端 Task 还没这个字段，退回 started_at / created_at。
        const beatAt = task.updated_at ?? task.started_at ?? task.created_at
        const heartbeat = formatSince(beatAt, now)
        const heartbeatLabel = task.updated_at ? '最近更新' : '已运行'
        const isStale = stale.has(task.id)
        return (
          <div className="task-item" key={task.id}>
            <div className="task-head">
              <span className="task-kind">{task.kind_label || task.kind}</span>
              <Badge tone={taskStatusClass(task.status)}>
                {task.status_label || statusText(task.status)}
              </Badge>
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
              {task.status === 'failed' || task.status === 'partial' ? (
                onRetry ? (
                  <button
                    type="button"
                    className="btn-link"
                    disabled={retrying === task.id}
                    onClick={() => void handleRetry(task)}
                  >
                    {retrying === task.id ? '重试中…' : '重试'}
                  </button>
                ) : null
              ) : null}
            </div>

            {running ? (
              <div className="row" style={{ gap: 10 }}>
                <div style={{ flex: 1, maxWidth: 320 }}>
                  <ProgressBar value={task.progress} />
                </div>
                <span className="task-stage">
                  {task.stage ? `${task.stage}` : '处理中'} · {task.progress}%
                  <span className="task-heartbeat">
                    {' '}
                    · {heartbeatLabel} {heartbeat}
                  </span>
                </span>
              </div>
            ) : null}

            {/*
              无进展提示刻意做成浅色说明条而不是红色报错：任务还在后台跑，
              只是被别的写事务挡住了，显示成失败会让用户去点删除或反复重启应用。
            */}
            {isStale ? (
              <div className="task-hint" role="status">
                <span className="task-hint-icon">⏳</span>
                <span>{staleHintText(task.status)}</span>
              </div>
            ) : null}

            {task.status === 'success' && task.result && Object.keys(task.result).length > 0 ? (
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
function staleHintText(status: string): string {
  if (status === 'queued') {
    return '排队超过 30 秒仍未开始，可能在等待其它任务写入，可稍后再看（不是失败）。'
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
  }
  Object.entries(result).forEach(([k, v]) => {
    if (typeof v === 'number' || typeof v === 'string') {
      parts.push(`${map[k] ?? k}: ${v}`)
    }
  })
  return parts.join(' · ')
}

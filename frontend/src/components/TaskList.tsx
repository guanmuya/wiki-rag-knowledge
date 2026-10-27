import { useState } from 'react'
import type { Task } from '../types'
import { formatTime, isRunningStatus, taskStatusClass } from '../utils'
import { Badge, EmptyState, ProgressBar, Spinner } from './ui'

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

  if (loading && tasks.length === 0) {
    return (
      <div className="state-block inline">
        <Spinner size={16} />
        <span>任务加载中…</span>
      </div>
    )
  }

  if (error) {
    return (
      <div className="state-block inline" style={{ color: 'var(--c-danger)' }}>
        <span>任务加载失败：{error instanceof Error ? error.message : '请稍后重试'}</span>
      </div>
    )
  }

  if (tasks.length === 0) {
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
      {tasks.map((task) => (
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
            <span className="tiny muted nowrap">{formatTime(task.created_at)}</span>
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

          {isRunningStatus(task.status) ? (
            <div className="row" style={{ gap: 10 }}>
              <div style={{ flex: 1, maxWidth: 320 }}>
                <ProgressBar value={task.progress} />
              </div>
              <span className="task-stage">
                {task.stage ? `${task.stage}` : '处理中'} · {task.progress}%
              </span>
            </div>
          ) : null}

          {task.status === 'success' && task.result && Object.keys(task.result).length > 0 ? (
            <div className="tiny muted">{resultText(task.result)}</div>
          ) : null}

          {task.error ? <div className="task-error">{task.error}</div> : null}

          {task.attempts > 1 ? (
            <div className="tiny muted">已执行 {task.attempts} 次</div>
          ) : null}
        </div>
      ))}
    </div>
  )
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

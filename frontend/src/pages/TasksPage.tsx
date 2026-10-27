import { useCallback, useEffect, useState } from 'react'
import { useToast } from '../auth'
import { kbApi, taskApi } from '../api'
import type { KnowledgeBase, Task } from '../types'
import { errorText, isRunningStatus, usePolling } from '../utils'
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  Select,
  SkeletonLines,
  Spinner,
} from '../components/ui'
import TaskList from '../components/TaskList'

const STATUS_OPTIONS: Array<{ value: string; label: string }> = [
  { value: '', label: '全部状态' },
  { value: 'queued,running', label: '进行中' },
  { value: 'success', label: '成功' },
  { value: 'partial', label: '部分失败' },
  { value: 'failed', label: '失败' },
]

export default function TasksPage() {
  const { push } = useToast()
  const [tasks, setTasks] = useState<Task[]>([])
  const [kbs, setKbs] = useState<KnowledgeBase[]>([])
  const [status, setStatus] = useState('')
  const [kbId, setKbId] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<unknown>(null)
  const [lastUpdated, setLastUpdated] = useState<string>('')

  const load = useCallback(
    async (silent = false) => {
      if (!silent) setLoading(true)
      try {
        const data = await taskApi.list({
          status: status || undefined,
          kb_id: kbId ? Number(kbId) : undefined,
        })
        setTasks(data.items ?? [])
        setError(null)
        setLastUpdated(new Date().toLocaleTimeString('zh-CN'))
      } catch (err) {
        setError(err)
      } finally {
        if (!silent) setLoading(false)
      }
    },
    [status, kbId],
  )

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    let alive = true
    kbApi
      .list()
      .then((d) => {
        if (alive) setKbs(d.items ?? [])
      })
      .catch(() => {
        /* 知识库下拉失败不阻塞任务列表 */
      })
    return () => {
      alive = false
    }
  }, [])

  const hasRunning = tasks.some((t) => isRunningStatus(t.status))

  // 有运行中任务时才轮询，全部结束后自动停止
  usePolling(() => load(true), 2000, hasRunning)

  const retry = async (task: Task) => {
    await taskApi.retry(task.id)
    push('已提交重试', 'success')
    await load(true)
  }

  const counts = {
    running: tasks.filter((t) => isRunningStatus(t.status)).length,
    failed: tasks.filter((t) => t.status === 'failed' || t.status === 'partial').length,
    success: tasks.filter((t) => t.status === 'success').length,
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>任务中心</h1>
          <div className="sub">
            文档入库、索引重建与 Wiki 生成任务。运行中的任务每 2 秒自动刷新，全部结束后停止轮询。
          </div>
        </div>
        <div className="spacer" />
        <div className="page-head-actions">
          <Select
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            style={{ width: 140 }}
          >
            {STATUS_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </Select>
          <Select value={kbId} onChange={(e) => setKbId(e.target.value)} style={{ width: 180 }}>
            <option value="">全部知识库</option>
            {kbs.map((k) => (
              <option key={k.id} value={k.id}>
                {k.name}
              </option>
            ))}
          </Select>
          <Button variant="ghost" onClick={() => void load()}>
            刷新
          </Button>
        </div>
      </div>

      <div className="row row-wrap" style={{ marginBottom: 14, gap: 8 }}>
        <Badge tone={counts.running > 0 ? 'warn' : 'muted'}>
          {counts.running > 0 ? <Spinner size={9} /> : <span className="dot" />}
          进行中 {counts.running}
        </Badge>
        <Badge tone={counts.failed > 0 ? 'danger' : 'muted'}>失败 {counts.failed}</Badge>
        <Badge tone="success">成功 {counts.success}</Badge>
        <span className="tiny muted">
          共 {tasks.length} 条{lastUpdated ? ` · 更新于 ${lastUpdated}` : ''}
          {hasRunning ? ' · 自动刷新中' : ' · 已停止自动刷新'}
        </span>
      </div>

      <div className="card">
        <div className="card-body">
          {loading ? (
            <SkeletonLines rows={6} />
          ) : error ? (
            <ErrorState error={error} onRetry={() => void load()} />
          ) : tasks.length === 0 ? (
            <EmptyState
              title="没有符合条件的任务"
              hint="上传文档、重建索引或在模型设置中切换 Embedding 后，任务会出现在这里。"
            />
          ) : (
            <TaskList
              tasks={tasks}
              showKb
              onRetry={retry}
              onRetryError={(err) => push(errorText(err), 'error')}
            />
          )}
        </div>
      </div>
    </>
  )
}

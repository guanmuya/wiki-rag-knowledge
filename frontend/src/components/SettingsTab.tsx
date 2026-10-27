import { useCallback, useEffect, useState } from 'react'
import { kbApi, taskApi } from '../api'
import type { KbMode, KnowledgeBase, Task } from '../types'
import {
  errorText,
  formatTime,
  isRunningStatus,
  modeLabel,
  taskStatusClass,
  usePolling,
} from '../utils'
import {
  Badge,
  Button,
  ConfirmDialog,
  Field,
  Select,
  TextArea,
  TextInput,
} from '../components/ui'
import TaskList from './TaskList'

export default function SettingsTab({
  kb,
  isAdmin,
  canManage,
  onToast,
  onChanged,
}: {
  kb: KnowledgeBase
  isAdmin: boolean
  canManage: boolean
  onToast: (text: string, kind?: 'info' | 'success' | 'error') => void
  onChanged: () => void
}) {
  const [name, setName] = useState(kb.name)
  const [description, setDescription] = useState(kb.description)
  const [mode, setMode] = useState<KbMode>(kb.mode)
  const [saving, setSaving] = useState(false)

  const [confirmRebuild, setConfirmRebuild] = useState<'chunk' | 'wiki' | null>(null)
  const [rebuildBusy, setRebuildBusy] = useState(false)

  const [tasks, setTasks] = useState<Task[]>([])

  useEffect(() => {
    setName(kb.name)
    setDescription(kb.description)
    setMode(kb.mode)
  }, [kb.id, kb.name, kb.description, kb.mode])

  const loadTasks = useCallback(async () => {
    try {
      const data = await taskApi.list({ kb_id: kb.id })
      const items = data.items ?? []
      setTasks(items.slice(0, 8))
    } catch {
      /* 设置页任务区静默失败 */
    }
  }, [kb.id])

  useEffect(() => {
    void loadTasks()
  }, [loadTasks])

  const hasRunning = tasks.some((t) => isRunningStatus(t.status))
  usePolling(loadTasks, 2000, hasRunning)

  const dirty = name !== kb.name || description !== kb.description || mode !== kb.mode

  const save = async () => {
    if (!name.trim()) {
      onToast('知识库名称不能为空', 'error')
      return
    }
    setSaving(true)
    try {
      await kbApi.update(kb.id, {
        name: name.trim(),
        description: description.trim(),
        mode,
      })
      onToast('设置已保存', 'success')
      onChanged()
    } catch (err) {
      onToast(errorText(err), 'error')
    } finally {
      setSaving(false)
    }
  }

  const doRebuild = async () => {
    if (!confirmRebuild) return
    setRebuildBusy(true)
    try {
      await kbApi.rebuild(kb.id, confirmRebuild)
      onToast(confirmRebuild === 'wiki' ? 'Wiki 重建任务已提交' : '分块索引重建任务已提交', 'success')
      setConfirmRebuild(null)
      await loadTasks()
      onChanged()
    } catch (err) {
      onToast(errorText(err), 'error')
    } finally {
      setRebuildBusy(false)
    }
  }

  const retryTask = async (task: Task) => {
    await taskApi.retry(task.id)
    onToast('已提交重试', 'success')
    await loadTasks()
  }

  return (
    <>
      <div className="settings-grid">
        <div className="card">
          <div className="card-head">
            <h3>基本信息</h3>
            {!isAdmin ? <span className="tiny muted">仅管理员可修改</span> : null}
          </div>
          <div className="card-body">
            <Field label="知识库名称" required>
              <TextInput
                value={name}
                onChange={(e) => setName(e.target.value)}
                disabled={!isAdmin}
                maxLength={128}
              />
            </Field>
            <Field label="描述">
              <TextArea
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                rows={3}
                disabled={!isAdmin}
                placeholder="说明该知识库收录的内容范围"
              />
            </Field>
            <Field
              label="检索模式"
              hint={
                mode === 'wiki'
                  ? 'Wiki 增强：问答优先检索 Wiki 章节，未就绪时回退到分块。切换后需要手动「重建 Wiki」。'
                  : '基础分块：问答按文本分块检索。'
              }
            >
              <Select
                value={mode}
                onChange={(e) => setMode(e.target.value as KbMode)}
                disabled={!isAdmin}
              >
                <option value="basic">基础分块</option>
                <option value="wiki">Wiki 增强</option>
              </Select>
            </Field>
            {isAdmin ? (
              <div className="row">
                <Button variant="primary" loading={saving} disabled={!dirty} onClick={() => void save()}>
                  保存修改
                </Button>
                <Button
                  variant="ghost"
                  disabled={!dirty || saving}
                  onClick={() => {
                    setName(kb.name)
                    setDescription(kb.description)
                    setMode(kb.mode)
                  }}
                >
                  撤销
                </Button>
                {dirty ? <span className="tiny muted">有未保存的修改</span> : null}
              </div>
            ) : null}
          </div>
        </div>

        <div className="col" style={{ gap: 16 }}>
          <div className="card">
            <div className="card-head">
              <h3>索引状态</h3>
            </div>
            <div className="card-body">
              <dl className="info-list">
                <dt>当前模式</dt>
                <dd>
                  <Badge tone={kb.mode === 'wiki' ? 'info' : 'muted'}>
                    {modeLabel(kb.mode, kb.mode_label)}
                  </Badge>
                </dd>
                <dt>库状态</dt>
                <dd>
                  <Badge tone={taskStatusClass(kb.status)}>{kb.status_label}</Badge>
                  {kb.status_detail ? <span className="tiny muted"> {kb.status_detail}</span> : null}
                </dd>
                <dt>分块索引</dt>
                <dd>
                  {kb.stats?.chunk_count ? (
                    <span className="test-ok">已建立 · {kb.stats.chunk_count} 个分块</span>
                  ) : (
                    <span className="muted">尚未建立</span>
                  )}
                </dd>
                <dt>Wiki 索引</dt>
                <dd>
                  {kb.wiki_ready ? (
                    <span className="test-ok">已就绪 · {kb.stats?.wiki_page_count ?? 0} 个页面</span>
                  ) : (
                    <span className="muted">未生成</span>
                  )}
                </dd>
                <dt>embed_signature</dt>
                <dd>
                  {kb.embed_signature ? (
                    <span className="sig-box">{kb.embed_signature}</span>
                  ) : (
                    <span className="muted">未绑定</span>
                  )}
                </dd>
                <dt>向量维度</dt>
                <dd>{kb.embed_dim || '—'}</dd>
                <dt>创建时间</dt>
                <dd>{formatTime(kb.created_at)}</dd>
                <dt>Wiki 就绪时间</dt>
                <dd>{formatTime(kb.wiki_ready_at ?? null)}</dd>
              </dl>

              {canManage ? (
                <>
                  <div className="divider" />
                  <div className="row row-wrap">
                    <Button
                      size="sm"
                      onClick={() => setConfirmRebuild('chunk')}
                      disabled={hasRunning}
                    >
                      重建分块索引
                    </Button>
                    <Button
                      size="sm"
                      onClick={() => setConfirmRebuild('wiki')}
                      disabled={hasRunning || kb.mode !== 'wiki'}
                      title={kb.mode !== 'wiki' ? '请先切换到 Wiki 增强模式' : undefined}
                    >
                      重建 Wiki
                    </Button>
                    {hasRunning ? <span className="tiny muted">有任务运行中…</span> : null}
                  </div>
                  {kb.mode !== 'wiki' ? (
                    <div className="tiny muted" style={{ marginTop: 8 }}>
                      当前为基础分块模式，重建 Wiki 前请先在左侧切换到 Wiki 增强并保存。
                    </div>
                  ) : null}
                </>
              ) : (
                <div className="tiny muted" style={{ marginTop: 12 }}>
                  重建索引需要该知识库的「可管理」权限。
                </div>
              )}
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <h3>最近任务</h3>
              <div className="spacer" />
              <Button size="sm" variant="ghost" onClick={() => void loadTasks()}>
                刷新
              </Button>
            </div>
            <div className="card-body">
              <TaskList
                tasks={tasks}
                onRetry={canManage ? retryTask : undefined}
                onRetryError={(err) => onToast(errorText(err), 'error')}
                emptyText="暂无任务记录"
              />
            </div>
          </div>
        </div>
      </div>

      <ConfirmDialog
        open={confirmRebuild !== null}
        title={confirmRebuild === 'wiki' ? '重建 Wiki' : '重建分块索引'}
        message={
          confirmRebuild === 'wiki' ? (
            <>
              将按当前文档重新归纳生成 Wiki 页面。已人工锁定的页面不会被覆盖；
              未锁定页面的自动内容会被替换。任务在后台运行，可在任务中心查看进度。
            </>
          ) : (
            <>
              将清空现有分块并重新解析全部文档，耗时取决于文档数量。
              期间问答可能检索不到该库内容。确认继续？
            </>
          )
        }
        confirmText="开始重建"
        busy={rebuildBusy}
        onConfirm={() => void doRebuild()}
        onCancel={() => setConfirmRebuild(null)}
      />
    </>
  )
}

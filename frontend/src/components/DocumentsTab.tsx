import { useCallback, useEffect, useRef, useState } from 'react'
import { docApi, taskApi } from '../api'
import type { ChunkPreview, DocumentItem, KnowledgeBase, Task } from '../types'
import {
  errorText,
  formatBytes,
  formatNumber,
  formatTime,
  isRunningStatus,
  kbStatusLabel,
  taskStatusClass,
  usePolling,
} from '../utils'
import {
  Badge,
  Button,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Modal,
  SkeletonLines,
  Spinner,
} from '../components/ui'
import TaskList from '../components/TaskList'

export default function DocumentsTab({
  kb,
  canManage,
  onChanged,
  onToast,
}: {
  kb: KnowledgeBase
  canManage: boolean
  onChanged: () => void
  onToast: (text: string, kind?: 'info' | 'success' | 'error') => void
}) {
  const [docs, setDocs] = useState<DocumentItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<unknown>(null)

  const [tasks, setTasks] = useState<Task[]>([])
  const [taskError, setTaskError] = useState<unknown>(null)
  const [tasksLoaded, setTasksLoaded] = useState(false)

  const [uploading, setUploading] = useState(false)
  const [uploadFiles, setUploadFiles] = useState<File[]>([])
  const [dragging, setDragging] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const [deleting, setDeleting] = useState<DocumentItem | null>(null)
  const [deleteBusy, setDeleteBusy] = useState(false)

  const [chunkDoc, setChunkDoc] = useState<DocumentItem | null>(null)
  const [chunks, setChunks] = useState<ChunkPreview[]>([])
  const [chunkTotal, setChunkTotal] = useState(0)
  const [chunkPage, setChunkPage] = useState(1)
  const [chunkLoading, setChunkLoading] = useState(false)
  const [chunkError, setChunkError] = useState<unknown>(null)

  const [versionDoc, setVersionDoc] = useState<DocumentItem | null>(null)
  const [versionFile, setVersionFile] = useState<File | null>(null)
  const [versionBusy, setVersionBusy] = useState(false)
  const [retryingDoc, setRetryingDoc] = useState<number | null>(null)
  const versionInputRef = useRef<HTMLInputElement>(null)

  const runningRef = useRef(false)

  const loadDocs = useCallback(async () => {
    try {
      const data = await docApi.list(kb.id)
      setDocs(data.items ?? [])
      setError(null)
    } catch (err) {
      setError(err)
    } finally {
      setLoading(false)
    }
  }, [kb.id])

  const loadTasks = useCallback(async () => {
    try {
      const data = await taskApi.list({ kb_id: kb.id })
      const items = data.items ?? []
      setTasks(items)
      setTaskError(null)
      const running = items.some((t) => isRunningStatus(t.status))
      if (runningRef.current && !running) {
        // 刚刚跑完：刷新文档与知识库统计
        void loadDocs()
        onChanged()
      }
      runningRef.current = running
    } catch (err) {
      setTaskError(err)
    } finally {
      setTasksLoaded(true)
    }
  }, [kb.id, loadDocs, onChanged])

  useEffect(() => {
    setLoading(true)
    void loadDocs()
    void loadTasks()
  }, [loadDocs, loadTasks])

  const hasRunning = tasks.some((t) => isRunningStatus(t.status))
  usePolling(loadTasks, 2000, hasRunning || uploading)

  // 同一份文档正在跑的任务：有它就不该再给重试入口（后端也会 409）
  const activeDocIds = new Set(
    tasks.filter((t) => t.doc_id && isRunningStatus(t.status)).map((t) => t.doc_id as number),
  )

  /**
   * 哪些文档可以重试：
   * - 失败 / 部分失败：正常重试；
   * - 「索引中」但已经没有任何排队或执行中的任务：卡死的状态，给一条出路；
   * - 有任务在跑、或已经成功可用的：不给，避免重复排队。
   */
  const docRetryable = (doc: DocumentItem) => {
    if (!tasksLoaded || activeDocIds.has(doc.id)) return false
    const latest = doc.latest_version ?? doc.current_version
    const status = latest?.status ?? doc.status
    return status === 'failed' || status === 'partial' || status === 'indexing'
  }

  /**
   * 同一份文档只展示最近的一条入库任务。
   * 重试已经复用同一行，重复行只会来自旧数据；这里折叠掉，避免
   * 「一份文档三条记录」的视觉噪音。完整历史去任务中心看。
   */
  const visibleTasks = tasks.filter((task, index) => {
    if (task.kind !== 'ingest_document' || !task.doc_id) return true
    return (
      tasks.findIndex((o) => o.kind === 'ingest_document' && o.doc_id === task.doc_id) === index
    )
  })
  const hiddenTaskCount = tasks.length - visibleTasks.length

  const addFiles = (files: FileList | File[]) => {
    const list = Array.from(files)
    if (list.length === 0) return
    setUploadFiles((prev) => {
      const names = new Set(prev.map((f) => `${f.name}:${f.size}`))
      return [...prev, ...list.filter((f) => !names.has(`${f.name}:${f.size}`))]
    })
  }

  const doUpload = async () => {
    if (uploadFiles.length === 0) return
    setUploading(true)
    try {
      const res = await docApi.upload(kb.id, uploadFiles)
      const okCount = res.items?.length ?? 0
      if (okCount > 0) onToast(`已上传 ${okCount} 个文档，正在后台解析入库`, 'success')
      if (res.errors?.length) {
        const text = res.errors
          .map((e) => (typeof e === 'string' ? e : `${e.filename}：${e.error}`))
          .join('；')
        onToast(`部分文件未通过：${text}`, 'error')
      }
      setUploadFiles([])
      if (inputRef.current) inputRef.current.value = ''
      await loadDocs()
      await loadTasks()
      onChanged()
    } catch (err) {
      onToast(errorText(err), 'error')
    } finally {
      setUploading(false)
    }
  }

  const doDelete = async () => {
    if (!deleting) return
    setDeleteBusy(true)
    try {
      await docApi.remove(deleting.id)
      onToast('文档已删除', 'success')
      setDeleting(null)
      await loadDocs()
      await loadTasks()
      onChanged()
    } catch (err) {
      onToast(errorText(err), 'error')
    } finally {
      setDeleteBusy(false)
    }
  }

  const doDownload = async (doc: DocumentItem, versionId?: number) => {
    try {
      const blob = await docApi.download(doc.id, versionId)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = doc.filename
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      window.setTimeout(() => URL.revokeObjectURL(url), 4000)
    } catch (err) {
      onToast(`下载失败：${errorText(err)}`, 'error')
    }
  }

  const openChunks = async (doc: DocumentItem, page = 1) => {
    setChunkDoc(doc)
    setChunkPage(page)
    setChunkLoading(true)
    setChunkError(null)
    try {
      const data = await docApi.chunks(doc.id, page, 10)
      setChunks(data.items ?? [])
      setChunkTotal(data.total ?? 0)
    } catch (err) {
      setChunkError(err)
      setChunks([])
      setChunkTotal(0)
    } finally {
      setChunkLoading(false)
    }
  }

  const doUploadVersion = async () => {
    if (!versionDoc || !versionFile) return
    setVersionBusy(true)
    try {
      await docApi.uploadVersion(versionDoc.id, versionFile)
      onToast('新版本已上传，正在重新索引', 'success')
      setVersionDoc(null)
      setVersionFile(null)
      if (versionInputRef.current) versionInputRef.current.value = ''
      await loadDocs()
      await loadTasks()
      onChanged()
    } catch (err) {
      onToast(errorText(err), 'error')
    } finally {
      setVersionBusy(false)
    }
  }

  /**
   * 重试的是「这份文档的入库」，不是「某一条任务记录」。
   * 后端会复用该文档最新版本对应的那条任务原地重排，所以点多少次都只有一条记录，
   * 并且会同步把文档状态打回「索引中」——上下两处不会再各说各话。
   */
  const retryDocument = async (doc: DocumentItem) => {
    setRetryingDoc(doc.id)
    try {
      const res = await docApi.reindex(doc.id)
      onToast(`任务 #${res.task_id} 已重新排队（第 ${res.retry_count || 1} 次）`, 'success')
      await loadDocs()
      await loadTasks()
      onChanged()
    } catch (err) {
      onToast(errorText(err), 'error')
    } finally {
      setRetryingDoc(null)
    }
  }
  const chunkPages = Math.max(1, Math.ceil(chunkTotal / 10))

  return (
    <>
      {canManage ? (
        <div style={{ marginBottom: 18 }}>
          <div
            className={`dropzone ${dragging ? 'dragging' : ''}`}
            onClick={() => inputRef.current?.click()}
            onDragOver={(e) => {
              e.preventDefault()
              setDragging(true)
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault()
              setDragging(false)
              if (e.dataTransfer.files?.length) addFiles(e.dataTransfer.files)
            }}
          >
            <div className="dropzone-title">
              {uploading ? '正在上传…' : '点击选择文件，或把文件拖到这里'}
            </div>
            <div className="dropzone-hint">
              支持多选；格式：pdf / docx / xlsx / pptx / md / txt / csv / json / html / log
            </div>
            <input
              ref={inputRef}
              type="file"
              multiple
              hidden
              onChange={(e) => {
                if (e.target.files) addFiles(e.target.files)
              }}
            />
          </div>

          {uploadFiles.length > 0 ? (
            <>
              <div className="upload-list">
                {uploadFiles.map((f, i) => (
                  <div className="upload-item" key={`${f.name}-${i}`}>
                    <span className="truncate" style={{ flex: 1 }}>
                      {f.name}
                    </span>
                    <span className="muted tiny nowrap">{formatBytes(f.size)}</span>
                    <button
                      type="button"
                      className="btn-link danger"
                      onClick={() => setUploadFiles((prev) => prev.filter((_, idx) => idx !== i))}
                    >
                      移除
                    </button>
                  </div>
                ))}
              </div>
              <div className="row" style={{ marginTop: 10 }}>
                <Button variant="primary" loading={uploading} onClick={() => void doUpload()}>
                  上传 {uploadFiles.length} 个文件
                </Button>
                <Button
                  variant="ghost"
                  onClick={() => {
                    setUploadFiles([])
                    if (inputRef.current) inputRef.current.value = ''
                  }}
                  disabled={uploading}
                >
                  清空
                </Button>
              </div>
            </>
          ) : null}
        </div>
      ) : (
        <div className="state-block inline" style={{ justifyContent: 'flex-start', padding: '0 0 14px' }}>
          <span className="muted">你对该知识库只有只读权限，上传与删除操作需要「可管理」等级。</span>
        </div>
      )}

      <div className="card" style={{ marginBottom: 18 }}>
        <div className="card-head">
          <h3>文档列表</h3>
          <span className="tiny muted">{docs.length} 个文档</span>
          <div className="spacer" />
          <Button size="sm" variant="ghost" onClick={() => void loadDocs()}>
            刷新
          </Button>
        </div>
        <div className="card-body card-body-flush">
          {/*
            刷新失败但已经有文档时，保留表格 + 顶部提示条：
            后端被写锁挡住时清空表格，用户会以为文档被删了。
          */}
          {loading && docs.length === 0 ? (
            <div style={{ padding: 16 }}>
              <SkeletonLines rows={4} />
            </div>
          ) : docs.length === 0 ? (
            error ? (
              <ErrorState error={error} onRetry={() => void loadDocs()} />
            ) : (
              <EmptyState
                title="还没有文档"
                hint={canManage ? '拖拽或点击上方区域上传第一个文档。' : '管理员尚未向该知识库上传文档。'}
              />
            )
          ) : (
            <>
              {error ? (
                <div className="poll-notice" style={{ margin: '12px 16px 0' }} role="status">
                  <span className="poll-notice-icon">⚠</span>
                  <div className="poll-notice-body">
                    <strong>文档列表刷新失败，下方仍是上次获取到的数据</strong>
                    <div className="poll-notice-detail">{errorText(error)}</div>
                  </div>
                </div>
              ) : null}
              <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>文件名</th>
                    <th>版本</th>
                    <th>状态</th>
                    <th>分块</th>
                    <th>大小</th>
                    <th>上传时间</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {docs.map((doc) => {
                    // 状态以「最新上传的版本」为准：current_version 是已生效的那一版，
                    // 新版本还在解析或已失败时它仍是旧的，只看它会出现
                    // 「任务失败、文档却显示成功」的矛盾。
                    const latest = doc.latest_version ?? doc.current_version
                    const status = latest?.status ?? doc.status
                    // 最新版本已经成功时，不再展示文档行上遗留的旧失败说明
                    const detail =
                      latest?.status_detail ||
                      (latest?.status === 'success' ? '' : doc.status_detail)
                    return (
                    <tr key={doc.id}>
                      <td>
                        <div className="truncate strong" style={{ maxWidth: 360 }} title={doc.filename}>
                          {doc.filename}
                        </div>
                        {detail ? (
                          <div className="tiny muted truncate" style={{ maxWidth: 360 }} title={detail}>
                            {detail}
                          </div>
                        ) : null}
                      </td>
                      <td className="nowrap">
                        {latest ? `v${latest.version_no}` : '—'}
                      </td>
                      <td>
                        <Badge tone={taskStatusClass(status)}>
                          {kbStatusLabel(status, latest?.status_label ?? doc.status_label)}
                        </Badge>
                      </td>
                      <td>{formatNumber(doc.current_version?.chunk_count)}</td>
                      <td className="muted nowrap">{formatBytes(doc.size_bytes)}</td>
                      <td className="muted small nowrap">{formatTime(doc.created_at)}</td>
                      <td className="actions">
                        <div className="table-actions">
                          <button
                            type="button"
                            className="btn-link"
                            onClick={() => void doDownload(doc, doc.current_version?.id ?? latest?.id)}
                          >
                            下载
                          </button>
                          <button type="button" className="btn-link" onClick={() => void openChunks(doc)}>
                            分块预览
                          </button>
                          {canManage && docRetryable(doc) ? (
                            <button
                              type="button"
                              className="btn-link"
                              disabled={retryingDoc === doc.id}
                              title="重新排队解析这份文档（复用同一条任务记录）"
                              onClick={() => void retryDocument(doc)}
                            >
                              {retryingDoc === doc.id ? '提交中…' : '重试'}
                            </button>
                          ) : null}
                          {canManage ? (
                            <>
                              <button
                                type="button"
                                className="btn-link"
                                onClick={() => {
                                  setVersionDoc(doc)
                                  setVersionFile(null)
                                }}
                              >
                                上传新版本
                              </button>
                              <button
                                type="button"
                                className="btn-link danger"
                                onClick={() => setDeleting(doc)}
                              >
                                删除
                              </button>
                            </>
                          ) : null}
                        </div>
                      </td>
                    </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
            </>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <h3>任务列表</h3>
          {hasRunning ? (
            <span className="tiny muted row" style={{ gap: 5 }}>
              <Spinner size={11} /> 运行中，每 2 秒自动刷新
            </span>
          ) : (
            <span className="tiny muted">无运行中任务</span>
          )}
          <span className="tiny muted">· 只展示进度与历史，重试请在上方文档列表操作</span>
          {hiddenTaskCount > 0 ? (
            <span className="tiny muted">· 已折叠 {hiddenTaskCount} 条历史记录（任务中心可查）</span>
          ) : null}
          <div className="spacer" />
          <Button size="sm" variant="ghost" onClick={() => void loadTasks()}>
            刷新
          </Button>
        </div>
        <div className="card-body">
          <TaskList
            tasks={visibleTasks}
            loading={!tasksLoaded}
            error={taskError}
            emptyText="该知识库暂无任务"
          />
        </div>
      </div>

      <ConfirmDialog
        open={Boolean(deleting)}
        title="删除文档"
        message={
          <>
            确认删除「{deleting?.filename}」？该文档及其分块索引会被移除
            {kb.mode === 'wiki' ? '，并触发该库的 Wiki 重建任务' : ''}。此操作不可撤销。
          </>
        }
        confirmText="删除"
        danger
        busy={deleteBusy}
        onConfirm={() => void doDelete()}
        onCancel={() => setDeleting(null)}
      />

      <Modal
        open={Boolean(chunkDoc)}
        title={`分块预览 · ${chunkDoc?.filename ?? ''}`}
        onClose={() => setChunkDoc(null)}
        width={760}
        footer={
          <>
            <span className="tiny muted" style={{ marginRight: 'auto' }}>
              共 {formatNumber(chunkTotal)} 个分块
            </span>
            <Button
              size="sm"
              disabled={chunkPage <= 1 || chunkLoading}
              onClick={() => chunkDoc && void openChunks(chunkDoc, chunkPage - 1)}
            >
              上一页
            </Button>
            <span className="small muted" style={{ alignSelf: 'center' }}>
              {chunkPage} / {chunkPages}
            </span>
            <Button
              size="sm"
              disabled={chunkPage >= chunkPages || chunkLoading}
              onClick={() => chunkDoc && void openChunks(chunkDoc, chunkPage + 1)}
            >
              下一页
            </Button>
          </>
        }
      >
        {chunkLoading ? (
          <SkeletonLines rows={6} />
        ) : chunkError ? (
          <ErrorState error={chunkError} />
        ) : chunks.length === 0 ? (
          <EmptyState title="暂无分块" hint="文档可能还在解析中，或解析后未产生文本分块。" />
        ) : (
          <div className="col" style={{ gap: 12 }}>
            {chunks.map((c, idx) => (
              <div key={c.id ?? idx} className="panel">
                <div className="panel-head">
                  <span>#{c.ordinal ?? c.index ?? (chunkPage - 1) * 10 + idx + 1}</span>
                  {c.locator ? <span className="tiny muted">· {c.locator}</span> : null}
                  {c.heading ? <span className="tiny muted">· {c.heading}</span> : null}
                  <div className="spacer" />
                  {c.dim || c.embed_dim || c.vector_dim ? (
                    <span className="tiny muted">
                      向量 {c.dim ?? c.embed_dim ?? c.vector_dim} 维
                      {c.has_vector === false ? '（无向量）' : ''}
                    </span>
                  ) : null}
                </div>
                <div className="panel-body">
                  <div style={{ whiteSpace: 'pre-wrap', fontSize: 12.5, lineHeight: 1.7 }}>
                    {c.text || c.content || '（无文本）'}
                  </div>
                  {c.keywords && c.keywords.length > 0 ? (
                    <div className="tiny muted" style={{ marginTop: 6 }}>
                      关键词：{c.keywords.join(' / ')}
                    </div>
                  ) : null}
                </div>
              </div>
            ))}
          </div>
        )}
      </Modal>

      <Modal
        open={Boolean(versionDoc)}
        title={`上传新版本 · ${versionDoc?.filename ?? ''}`}
        onClose={() => setVersionDoc(null)}
        width={480}
        footer={
          <>
            <Button variant="ghost" onClick={() => setVersionDoc(null)} disabled={versionBusy}>
              取消
            </Button>
            <Button
              variant="primary"
              loading={versionBusy}
              disabled={!versionFile}
              onClick={() => void doUploadVersion()}
            >
              上传并重建索引
            </Button>
          </>
        }
      >
        <div className="confirm-text" style={{ marginBottom: 12 }}>
          上传后生成新的版本记录，旧版本仍保留在版本历史中，可用于对比与回溯。
        </div>
        <input
          ref={versionInputRef}
          type="file"
          onChange={(e) => setVersionFile(e.target.files?.[0] ?? null)}
        />
        {versionFile ? (
          <div className="tiny muted" style={{ marginTop: 8 }}>
            已选择：{versionFile.name}（{formatBytes(versionFile.size)}）
          </div>
        ) : null}
      </Modal>
    </>
  )
}

// 统一 HTTP 封装：注入 Bearer 令牌、401 清空登录态、403 中文 toast。
import type {
  AskResponse,
  AuditEntry,
  ChatMessage,
  Conversation,
  ConversationDetail,
  CreateUserPayload,
  DocumentDetail,
  DocumentItem,
  EmbeddingStatus,
  EmbeddingSwitchResult,
  HealthInfo,
  KnowledgeBase,
  LoginResponse,
  MeResponse,
  MetaInfo,
  SetupAdminPayload,
  SetupStatus,
  ModelConfig,
  ModelImportItem,
  ModelImportResult,
  ModelPayload,
  ModelTestResult,
  DiscoverModelsResult,
  Paged,
  Permissions,
  Provider,
  ProviderPayload,
  RouteInfo,
  SearchPreviewRaw,
  SearchPreviewResponse,
  Task,
  UpdateUserPayload,
  UploadResponse,
  User,
  WikiEvidenceRef,
  WikiPage,
  WikiPageDetail,
  WikiPageVersion,
  WikiTree,
  ChunkPreview,
  KbMode,
  GrantLevel,
  ModelPurpose,
} from './types'

export const TOKEN_KEY = 'wrk_token'

/**
 * 后端基地址。
 * - 桌面版：Electron 主进程动态分配端口，通过 preload 注入，必须在启动时取一次。
 * - 浏览器开发模式：留空走相对路径 `/api`，由 Vite 代理转发。
 */
let apiBase = ''
let baseReady: Promise<void> | null = null

declare global {
  interface Window {
    wrkDesktop?: {
      isDesktop: true
      getInfo: () => Promise<{ apiBase: string; version: string; backendVersion: string; dataDir: string }>
      getLogs: () => Promise<string>
      openPath: (kind: 'data' | 'logs') => Promise<string>
      restartBackend: () => Promise<{ ok: boolean; port?: number; error?: string }>
      updateState?: () => Promise<import('./types').UpdateState>
      checkUpdate?: () => Promise<{ ok: boolean; message?: string }>
      installUpdate?: () => Promise<{ ok: boolean; message?: string }>
      onUpdate?: (cb: (state: import('./types').UpdateState) => void) => () => void
    }
  }
}

export function isDesktop(): boolean {
  return Boolean(window.wrkDesktop?.isDesktop)
}

/** 桌面版启动时调用一次；浏览器模式立即返回。 */
export function initApiBase(): Promise<void> {
  if (baseReady) return baseReady
  baseReady = (async () => {
    if (!window.wrkDesktop) return
    try {
      const info = await window.wrkDesktop.getInfo()
      apiBase = info.apiBase || ''
    } catch {
      apiBase = ''
    }
  })()
  return baseReady
}

export function getApiBase(): string {
  return apiBase
}

function apiUrl(path: string): string {
  return apiBase ? `${apiBase}/api${path}` : `/api${path}`
}

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* 隐私模式下忽略 */
  }
}

export class ApiError extends Error {
  status: number
  detail: string

  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

type UnauthorizedHandler = () => void
type ForbiddenHandler = (detail: string) => void

let onUnauthorized: UnauthorizedHandler = () => {}
let onForbidden: ForbiddenHandler = () => {}

export function configureApiHandlers(handlers: {
  onUnauthorized?: UnauthorizedHandler
  onForbidden?: ForbiddenHandler
}): void {
  if (handlers.onUnauthorized) onUnauthorized = handlers.onUnauthorized
  if (handlers.onForbidden) onForbidden = handlers.onForbidden
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  body?: unknown
  /** 直接传 FormData 时使用，不做 JSON 序列化 */
  form?: FormData
  /** 登录接口等场景：401 不触发全局登出 */
  silent401?: boolean
  signal?: AbortSignal
}

async function parseError(res: Response): Promise<string> {
  try {
    const data: unknown = await res.json()
    if (data && typeof data === 'object' && 'detail' in data) {
      const detail = (data as { detail: unknown }).detail
      if (typeof detail === 'string') return detail
      if (Array.isArray(detail)) {
        return detail
          .map((d) => {
            if (d && typeof d === 'object' && 'msg' in d) return String((d as { msg: unknown }).msg)
            return String(d)
          })
          .join('；')
      }
      return JSON.stringify(detail)
    }
    return `请求失败（HTTP ${res.status}）`
  } catch {
    return `请求失败（HTTP ${res.status}）`
  }
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  if (baseReady) await baseReady
  const headers: Record<string, string> = { Accept: 'application/json' }
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`

  let body: BodyInit | undefined
  if (opts.form) {
    body = opts.form
  } else if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(opts.body)
  }

  let res: Response
  try {
    res = await fetch(apiUrl(path), {
      method: opts.method ?? 'GET',
      headers,
      body,
      signal: opts.signal,
    })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err
    throw new ApiError(0, '无法连接服务器，请检查后端是否已启动')
  }

  if (res.status === 401) {
    const detail = await parseError(res)
    if (!opts.silent401) {
      setToken(null)
      onUnauthorized()
    }
    throw new ApiError(401, detail)
  }

  if (res.status === 403) {
    const detail = await parseError(res)
    onForbidden(detail || '没有操作权限')
    throw new ApiError(403, detail)
  }

  if (!res.ok) {
    throw new ApiError(res.status, await parseError(res))
  }

  if (res.status === 204) return undefined as T
  const text = await res.text()
  if (!text) return undefined as T
  try {
    return JSON.parse(text) as T
  } catch {
    return text as unknown as T
  }
}

async function requestBlob(path: string): Promise<Blob> {
  if (baseReady) await baseReady
  const headers: Record<string, string> = {}
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  const res = await fetch(apiUrl(path), { headers })
  if (res.status === 401) {
    setToken(null)
    onUnauthorized()
    throw new ApiError(401, '登录状态失效，请重新登录')
  }
  if (res.status === 403) {
    const detail = await parseError(res)
    onForbidden(detail)
    throw new ApiError(403, detail)
  }
  if (!res.ok) throw new ApiError(res.status, await parseError(res))
  return res.blob()
}

function qs(params: Record<string, string | number | undefined | null>): string {
  const sp = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') sp.set(k, String(v))
  })
  const s = sp.toString()
  return s ? `?${s}` : ''
}

// ---------------------------------------------------------------- 认证
export const authApi = {
  login: (username: string, password: string) =>
    request<LoginResponse>('/auth/login', {
      method: 'POST',
      body: { username, password },
      silent401: true,
    }),
  me: () => request<MeResponse>('/auth/me'),
  changePassword: (old_password: string, new_password: string) =>
    request<{ ok: boolean }>('/auth/change-password', {
      method: 'POST',
      body: { old_password, new_password },
      silent401: true,
    }),
  logout: () => request<{ ok: boolean }>('/auth/logout', { method: 'POST' }),
}

export const metaApi = {
  get: () => request<MetaInfo>('/meta'),
  health: () => request<HealthInfo>('/health'),
}

// ---------------------------------------------------------------- 首次初始化
export const setupApi = {
  status: () => request<SetupStatus>('/setup/status'),
  createAdmin: (payload: SetupAdminPayload) =>
    request<{ ok: boolean; user_id: number; username: string }>('/setup/admin', {
      method: 'POST',
      body: payload,
      // 初始化页面本身没有登录态，401 不应触发全局登出跳转
      silent401: true,
    }),
  getOrg: () => request<{ org_name: string }>('/setup/org'),
}

/**
 * 初始化完成事件。
 *
 * 为什么需要它：`BackendGate` 在启动时缓存了 `needs_setup`。初始化成功后若只
 * navigate 到 /login，门里的缓存仍是 true，会把用户又弹回 /setup；
 * SetupPage 再检测到已初始化跳回 /login —— 来回死循环，页面停在
 * 「正在检查系统状态…」。所以初始化成功后必须让门重新探活。
 */
export const SETUP_COMPLETED_EVENT = 'wrk:setup-completed'

export function notifySetupCompleted(): void {
  window.dispatchEvent(new Event(SETUP_COMPLETED_EVENT))
}

// ---------------------------------------------------------------- 用户
export const usersApi = {
  list: () => request<Paged<User>>('/users'),
  create: (payload: CreateUserPayload) =>
    request<User>('/users', { method: 'POST', body: payload }),
  update: (id: number, payload: UpdateUserPayload) =>
    request<User>(`/users/${id}`, { method: 'PATCH', body: payload }),
  setGrants: (id: number, grants: Array<{ kb_id: number; level: GrantLevel }>) =>
    request<{ ok: boolean }>(`/users/${id}/grants`, { method: 'PUT', body: { grants } }),
  resetPassword: (id: number, new_password: string) =>
    request<{ ok: boolean }>(`/users/${id}/reset-password`, {
      method: 'POST',
      body: { new_password },
    }),
  remove: (id: number) => request<{ ok: boolean }>(`/users/${id}`, { method: 'DELETE' }),
}

// ---------------------------------------------------------------- 知识库
export const kbApi = {
  list: () => request<Paged<KnowledgeBase>>('/kbs'),
  get: (id: number) => request<KnowledgeBase>(`/kbs/${id}`),
  create: (payload: { name: string; description: string; mode: KbMode }) =>
    request<KnowledgeBase>('/kbs', { method: 'POST', body: payload }),
  update: (id: number, payload: { name?: string; description?: string; mode?: KbMode }) =>
    request<KnowledgeBase>(`/kbs/${id}`, { method: 'PATCH', body: payload }),
  remove: (id: number) => request<{ ok: boolean }>(`/kbs/${id}`, { method: 'DELETE' }),
  rebuild: (id: number, kind: 'chunk' | 'wiki') =>
    request<{ task_id: number }>(`/kbs/${id}/rebuild`, { method: 'POST', body: { kind } }),
  grants: (id: number) =>
    request<Array<{ user_id: number; username: string; display_name: string; level: GrantLevel }>>(
      `/kbs/${id}/grants`,
    ),
}

// ---------------------------------------------------------------- 文档
export const docApi = {
  list: (kbId: number) => request<Paged<DocumentItem>>(`/kbs/${kbId}/documents`),
  detail: (docId: number) => request<DocumentDetail>(`/documents/${docId}`),
  upload: (kbId: number, files: File[]) => {
    const form = new FormData()
    files.forEach((f) => form.append('files', f))
    return request<UploadResponse>(`/kbs/${kbId}/documents`, { method: 'POST', form })
  },
  uploadVersion: (docId: number, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<{ version_id: number; task_id: number | null }>(
      `/documents/${docId}/versions`,
      { method: 'POST', form },
    )
  },
  chunks: (docId: number, page = 1, pageSize = 10) =>
    request<Paged<ChunkPreview>>(
      `/documents/${docId}/chunks${qs({ page, page_size: pageSize })}`,
    ),
  remove: (docId: number) => request<{ ok: boolean }>(`/documents/${docId}`, { method: 'DELETE' }),
  download: (docId: number, versionId?: number) =>
    requestBlob(`/documents/${docId}/download${qs({ version_id: versionId })}`),
}

// ---------------------------------------------------------------- 任务
export const taskApi = {
  list: (params: { status?: string; kb_id?: number } = {}) =>
    request<Paged<Task>>(`/tasks${qs({ status: params.status, kb_id: params.kb_id })}`),
  get: (id: number) => request<Task>(`/tasks/${id}`),
  retry: (id: number) => request<{ task_id: number }>(`/tasks/${id}/retry`, { method: 'POST' }),
}

// ---------------------------------------------------------------- Wiki
export const wikiApi = {
  /** 后端返回 {items,...}，这里归一化为 {pages,...} */
  tree: async (kbId: number): Promise<WikiTree> => {
    const raw = await request<WikiTree & { items?: WikiPage[] }>(`/kbs/${kbId}/wiki/tree`)
    return { ...raw, pages: raw.pages ?? raw.items ?? [] }
  },
  /** 后端把 page 字段平铺在顶层，这里归一化为 {page, version, evidence, related} */
  page: async (kbId: number, slug: string): Promise<WikiPageDetail> => {
    const raw = await request<
      (WikiPageDetail & { page?: WikiPage }) | (WikiPage & { version?: WikiPageVersion })
    >(`/kbs/${kbId}/wiki/pages/${encodeURIComponent(slug)}`)
    if ('page' in raw && raw.page) return raw as WikiPageDetail
    const flat = raw as WikiPage & { version?: WikiPageVersion; evidence?: WikiEvidenceRef[]; related?: WikiPage[] }
    return {
      page: flat,
      version: flat.version ?? null,
      evidence: flat.evidence ?? [],
      related: flat.related ?? [],
    }
  },
  save: (
    kbId: number,
    slug: string,
    content_md: string,
    summary?: string,
    note?: string,
  ) =>
    request<{ ok: boolean; version_id: number; version_no: number; human_locked?: boolean }>(
      `/kbs/${kbId}/wiki/pages/${encodeURIComponent(slug)}`,
      { method: 'PUT', body: { content_md, summary, note } },
    ),
  versions: (kbId: number, slug: string) =>
    request<{ items: WikiPageVersion[] } | WikiPageVersion[]>(
      `/kbs/${kbId}/wiki/pages/${encodeURIComponent(slug)}/versions`,
    ),
  publish: (kbId: number, slug: string) =>
    request<{ ok: boolean; status?: string }>(
      `/kbs/${kbId}/wiki/pages/${encodeURIComponent(slug)}/publish`,
      { method: 'POST' },
    ),
  unlock: (kbId: number, slug: string) =>
    request<{ ok: boolean; human_locked?: boolean }>(
      `/kbs/${kbId}/wiki/pages/${encodeURIComponent(slug)}/unlock`,
      { method: 'POST' },
    ),
}

// ---------------------------------------------------------------- 会话
export const chatApi = {
  conversations: () => request<Paged<Conversation>>('/conversations'),
  create: (title: string, kb_ids: number[]) =>
    request<Conversation>('/conversations', { method: 'POST', body: { title, kb_ids } }),
  get: (id: number) => request<ConversationDetail>(`/conversations/${id}`),
  update: (id: number, payload: { title?: string; kb_ids?: number[] }) =>
    request<Conversation>(`/conversations/${id}`, { method: 'PATCH', body: payload }),
  remove: (id: number) => request<{ ok: boolean }>(`/conversations/${id}`, { method: 'DELETE' }),
  ask: (id: number, question: string, kb_ids?: number[], top_k?: number) =>
    request<AskResponse>(`/conversations/${id}/ask`, {
      method: 'POST',
      body: { question, kb_ids, top_k },
    }),
  preview: async (kb_ids: number[], query: string): Promise<SearchPreviewResponse> => {
    const raw = await request<SearchPreviewRaw>('/search/preview', {
      method: 'POST',
      body: { kb_ids, query },
    })
    // 后端把 routes 直接返回成数组，这里归一化成与 ask 一致的 {kb_routes, stats}
    const routes: RouteInfo = {
      kb_routes: raw.routes ?? [],
      stats: raw.stats,
      denied: raw.denied,
    }
    return {
      evidence: raw.evidence ?? [],
      routes,
      stats: raw.stats,
      denied: raw.denied,
      context_preview: raw.context_preview,
    }
  },
}

// ---------------------------------------------------------------- 模型设置
export const settingsApi = {
  providers: () => request<Paged<Provider>>('/settings/providers'),
  createProvider: (payload: ProviderPayload) =>
    request<Provider>('/settings/providers', { method: 'POST', body: payload }),
  updateProvider: (id: number, payload: Partial<ProviderPayload>) =>
    request<Provider>(`/settings/providers/${id}`, { method: 'PATCH', body: payload }),
  removeProvider: (id: number) =>
    request<{ ok: boolean }>(`/settings/providers/${id}`, { method: 'DELETE' }),
  /**
   * 探测供应商可用模型。
   * 后端约定：探测失败也返回 200 + {items:[], error}，不抛 500，
   * 所以这里不需要额外 try —— 失败原因在 result.error 里。
   */
  discoverModels: (id: number) =>
    request<DiscoverModelsResult>(`/settings/providers/${id}/discover`, { method: 'POST' }),
  /** 批量导入模型配置；后端幂等，已存在的会进 skipped。 */
  importModels: (id: number, items: ModelImportItem[]) =>
    request<ModelImportResult>(`/settings/providers/${id}/models/import`, {
      method: 'POST',
      body: { items },
    }),

  models: () => request<Paged<ModelConfig>>('/settings/models'),
  createModel: (payload: ModelPayload) =>
    request<ModelConfig>('/settings/models', { method: 'POST', body: payload }),
  updateModel: (id: number, payload: Partial<ModelPayload>) =>
    request<ModelConfig>(`/settings/models/${id}`, { method: 'PATCH', body: payload }),
  removeModel: (id: number) =>
    request<{ ok: boolean }>(`/settings/models/${id}`, { method: 'DELETE' }),
  testModel: (id: number) =>
    request<ModelTestResult>(`/settings/models/${id}/test`, { method: 'POST' }),

  embeddingStatus: async (): Promise<EmbeddingStatus> => {
    const raw = await request<EmbeddingStatus>('/settings/embedding/status')
    const active = raw.active
    const running = (raw as { running_tasks?: unknown[] }).running_tasks
    const kbs = (raw.kbs ?? []).map((k) => ({
      ...k,
      consistent: k.consistent ?? !(k as { mismatch?: boolean }).mismatch,
    }))
    return {
      ...raw,
      kbs,
      consistent: raw.consistent ?? kbs.every((k) => k.consistent),
      rebuilding: raw.rebuilding ?? Boolean(running && running.length > 0),
      current_model_config_id: raw.current_model_config_id ?? active?.model_config_id ?? null,
      current_model: raw.current_model ?? active?.model,
      current_provider: raw.current_provider ?? active?.provider,
      current_signature: raw.current_signature ?? active?.signature,
      current_embed_dim: raw.current_embed_dim ?? active?.dim,
    }
  },
  switchEmbedding: async (model_config_id: number, confirm: boolean) => {
    const res = await request<EmbeddingSwitchResult>('/settings/embedding/switch', {
      method: 'POST',
      body: { model_config_id, confirm },
    })
    return {
      ...res,
      task_id: res.task_id ?? res.tasks?.[0]?.task_id ?? null,
    }
  },

  audit: (page = 1, pageSize = 20) =>
    request<Paged<AuditEntry>>(`/settings/audit${qs({ page, page_size: pageSize })}`),
}

/**
 * 探活：不依赖登录态，用于「后端未就绪」兜底页与关于页的「检查后端状态」。
 * 直接 fetch，不走 request()，避免 401 触发全局登出。
 */
export async function probeBackend(timeoutMs = 4000): Promise<{
  ok: boolean
  message: string
  version?: string
  offlineModels?: boolean
  latencyMs: number
}> {
  if (baseReady) await baseReady
  const started = performance.now()
  const ctrl = new AbortController()
  const timer = window.setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    const res = await fetch(apiUrl('/meta'), {
      headers: { Accept: 'application/json' },
      signal: ctrl.signal,
    })
    const latencyMs = Math.round(performance.now() - started)
    if (!res.ok) {
      return { ok: false, message: `后端返回 HTTP ${res.status}`, latencyMs }
    }
    const data = (await res.json()) as MetaInfo
    return {
      ok: true,
      message: '后端运行正常',
      version: data.version,
      offlineModels: data.offline_models,
      latencyMs,
    }
  } catch (err) {
    const latencyMs = Math.round(performance.now() - started)
    if (err instanceof DOMException && err.name === 'AbortError') {
      return { ok: false, message: '连接后端超时（4 秒无响应）', latencyMs }
    }
    return { ok: false, message: '无法连接后端服务', latencyMs }
  } finally {
    window.clearTimeout(timer)
  }
}

export type { Permissions, ChatMessage, ModelPurpose }

// 与 docs/API.md 对齐的类型定义。字段名以后端契约为准。

export type Role = 'admin' | 'employee'
export type UserStatus = 'active' | 'disabled'
export type KbMode = 'basic' | 'wiki'
export type KbStatus = 'empty' | 'indexing' | 'ready' | 'partial' | 'failed'
export type GrantLevel = 'read' | 'manage'
export type TaskStatus = 'queued' | 'running' | 'success' | 'partial' | 'failed'
export type TaskKind =
  | 'ingest_document'
  | 'rebuild_wiki'
  | 'reindex_kb'
  | 'embedding_rebuild'
export type WikiPageStatus = 'draft' | 'published' | 'needs_review'
export type ModelPurpose = 'llm' | 'embedding' | 'vision'

/**
 * 供应商 API 协议族。
 * - openai：/chat/completions + /embeddings（默认，兼容绝大多数中转）
 * - openai-responses：/responses
 * - anthropic：/messages
 */
export type ProviderProtocol = 'openai' | 'openai-responses' | 'anthropic'

export const PROTOCOL_OPTIONS: Array<{ value: ProviderProtocol; label: string; hint: string }> = [
  {
    value: 'openai',
    label: 'OpenAI 兼容',
    hint: 'POST /chat/completions 与 /embeddings，绝大多数中转与自建网关用这个。',
  },
  {
    value: 'openai-responses',
    label: 'OpenAI Responses',
    hint: 'POST /responses，新协议；不提供 Embedding 接口。',
  },
  {
    value: 'anthropic',
    label: 'Anthropic Messages',
    hint: 'POST /messages，system 独立字段；不提供 Embedding 接口。',
  },
]
export type ChatRole = 'user' | 'assistant'
export type RouteUsed = 'wiki' | 'chunk' | 'none'

/** 后端分页返回；部分接口（如 /users）只返回 items/total。 */
export interface Paged<T> {
  items: T[]
  total: number
  page?: number
  page_size?: number
}

export interface Grant {
  kb_id: number
  kb_name?: string
  level: GrantLevel
  level_label?: string
}

export interface User {
  id: number
  username: string
  display_name: string
  role: Role
  role_label?: string
  status: UserStatus
  status_label?: string
  must_change_password: boolean
  created_at: string | null
  last_login_at: string | null
  grants?: Grant[]
  grant_count?: number
}

export interface LoginResponse {
  token: string
  user: User
}

export interface Permissions {
  is_admin: boolean
  kb_grants: Array<{ kb_id: number; level: GrantLevel }>
}

export interface MeResponse {
  user: User
  permissions: Permissions
}

export interface MetaCapabilities {
  llm_configured: boolean
  embedding_configured: boolean
  worker_enabled: boolean
}

export interface MetaInfo {
  app_name: string
  initialized: boolean
  version: string
  offline_models: boolean
  /** 首次启动时为 true，前端据此跳初始化引导页 */
  needs_setup?: boolean
  /** 企业名称，显示在侧边栏与登录页 */
  org_name?: string
  capabilities?: MetaCapabilities
}

export interface SetupStatus {
  initialized: boolean
  org_name: string
  app_name: string
  version: string
}

export interface SetupAdminPayload {
  username: string
  password: string
  display_name?: string
  org_name?: string
}

export interface HealthInfo {
  ok: boolean
  version: string
}

/** 桌面端 preload 注入的只读信息（浏览器模式下不存在）。 */
export interface DesktopInfo {
  apiBase: string
  version: string
  backendVersion: string
  dataDir: string
}

export interface KbStats {
  doc_count: number
  chunk_count: number
  wiki_page_count: number
  wiki_published_count?: number
}

export interface KbIndexState {
  chunk_ready?: boolean
  wiki_ready?: boolean
  embed_signature?: string
}

export interface KnowledgeBase {
  id: number
  name: string
  description: string
  mode: KbMode
  status: KbStatus
  status_detail: string
  mode_label: string
  status_label: string
  embed_signature: string
  embed_dim: number
  wiki_ready: boolean
  my_level: GrantLevel | null
  created_at: string | null
  wiki_ready_at?: string | null
  stats: KbStats
  index?: KbIndexState
}

export interface DocumentVersion {
  id: number
  version_no: number
  status: TaskStatus
  status_label?: string
  status_detail: string
  chunk_count: number
  text_chars: number
  created_at: string | null
}

export interface DocumentItem {
  id: number
  kb_id: number
  filename: string
  suffix: string
  size_bytes: number
  mime: string
  status: KbStatus
  status_label?: string
  status_detail: string
  created_at: string | null
  current_version: DocumentVersion | null
  /** 最新上传的版本（可能还在解析或已失败），文档列表的状态以它为准 */
  latest_version?: DocumentVersion | null
}

export interface DocumentDetail extends DocumentItem {
  versions: DocumentVersion[]
}

export interface ChunkPreview {
  id: number
  ordinal?: number
  index?: number
  locator?: string
  heading?: string
  text?: string
  content?: string
  char_count?: number
  tokens?: number
  dim?: number
  embed_dim?: number
  vector_dim?: number
  embed_signature?: string
  has_vector?: boolean
  keywords?: string[]
}

export interface UploadResultItem {
  doc_id: number
  version_id?: number
  task_id: number | null
  filename?: string
}

export interface UploadError {
  filename: string
  error: string
}

export interface UploadResponse {
  items: UploadResultItem[]
  errors: UploadError[]
}

export interface Task {
  id: number
  kind: TaskKind
  kind_label: string
  status: TaskStatus
  status_label?: string
  stage: string
  progress: number
  kb_id: number | null
  kb_name: string
  doc_id: number | null
  doc_name: string
  doc_version_id?: number | null
  error: string
  attempts: number
  /** 手动重试次数（重试复用同一条任务记录，不再新增行） */
  retry_count?: number
  /** 上一次失败的报错，重试后仍保留，便于对照 */
  last_error?: string
  result: Record<string, unknown>
  created_at: string | null
  started_at: string | null
  /**
   * 任务心跳（最后一次进展的时间）。
   * 后端目前还没返回它（`task_out` 只有 created/started/finished），
   * 这里声明成可选：一旦后端补上，任务卡片的心跳会自动改用真实进展时间，
   * 前端不用再改一次；没有就退回 started_at。
   */
  updated_at?: string | null
  finished_at: string | null
}

export interface WikiPage {
  id: number
  kb_id: number
  slug: string
  title: string
  summary: string
  status: WikiPageStatus
  status_label?: string
  human_locked: boolean
  parent_slug: string | null
  order_index: number
  section_count: number
  current_version_id: number | null
  updated_at: string | null
}

export interface WikiTree {
  pages: WikiPage[]
  empty_reason?: string
  mode?: KbMode
  mode_label?: string
  wiki_ready?: boolean
}

export interface WikiEvidenceRef {
  chunk_id?: number
  doc_id?: number
  doc_name: string
  doc_version_id?: number
  version_no?: number
  locator: string
  quote: string
}

export interface WikiPageVersion {
  id: number
  version_no: number
  content_md: string
  origin: string
  summary?: string | null
  note?: string | null
  is_current?: boolean
  chars?: number
  created_at?: string | null
  author?: string
}

export interface WikiPageDetail {
  page: WikiPage
  version: WikiPageVersion | null
  evidence: WikiEvidenceRef[]
  related: WikiPage[]
}

export interface EvidenceItem {
  evidence_id: string
  kb_id: number
  kb_name: string
  source_type: 'chunk' | 'wiki'
  text: string
  score: number
  rank: number
  doc_name: string
  version_no: number
  locator: string
  heading: string | null
  page_title: string | null
  page_slug: string | null
  channels: Record<string, number>
  evidence: WikiEvidenceRef[]
  /** 查询词覆盖率 0~1，跨库唯一可比量（后端 retrieval.Evidence.to_dict 输出） */
  coverage?: number
  doc_id?: number | null
  chunk_id?: number | null
  page_id?: number | null
}

export interface KbRoute {
  kb_id: number
  kb_name: string
  mode: KbMode
  used: RouteUsed
  reason: string
  fallback: boolean
  keyword_hits: number
  vector_hits: number
  wiki_hits: number
  status?: string
}

export interface RouteStats {
  candidate_total: number
  final_total: number
  embedding_ok: boolean
  rrf_k: number
  /** 后端扩展字段：检索词、每库候选上限、库间覆盖分布 */
  query_terms?: string[]
  embedding_error?: string
  per_kb_limit?: number
  kb_coverage?: Record<string, number>
}

export interface RouteInfo {
  kb_routes: KbRoute[]
  stats: RouteStats
  /** search/preview 与 ask 在库被拒绝时携带 */
  denied?: DeniedKb[]
}

export interface UsageInfo {
  calls: number
  prompt_tokens: number
  completion_tokens: number
  latency_ms: number
  mode: string
  notes?: string[]
}

/** 拒答建议的类型：本次命中的资料 / 可直接问的问题。 */
export type SuggestionKind = 'resource' | 'question'

/**
 * 拒答时后端附带的单条建议，字段与 `backend/app/services/answering.py`
 * 的 `SuggestionBlock.items` 一一对应（`_resource_items` / `_question_items`）。
 * 本轮前端只对齐类型，不渲染。
 */
export interface SuggestionItem {
  /** 来源知识库名 */
  kb_name: string
  /** resource = 本次检索命中的资料；question = 建议问题 */
  kind: SuggestionKind
  /** 资料来源名（文档名 / Wiki 页面名 / 章节 heading），一定是库内真实文本 */
  title: string
  /** 仅 kind='question' 时有值：点击后填入输入框的问题 */
  question?: string
}

export interface ChatMessage {
  id: number
  role: ChatRole
  content: string
  kb_ids?: number[]
  evidence?: EvidenceItem[]
  route?: RouteInfo | null
  latency_ms?: number | null
  created_at: string | null
  /** 后端判定「这段回答是不是拒答」；历史消息与当次回答同口径（serializers.message_out） */
  insufficient?: boolean
  /** 拒答建议；老数据可能没有该字段，故为可选 */
  suggestions?: SuggestionItem[]
  /** 拒答原因；空串表示不是拒答 */
  refusal_reason?: string
  usage?: UsageInfo | null
}

export interface Conversation {
  id: number
  title: string
  kb_ids: number[]
  message_count: number
  created_at: string | null
  updated_at: string | null
}

export interface ConversationDetail extends Conversation {
  messages: ChatMessage[]
}

export interface DeniedKb {
  kb_id: number
  kb_name: string
  reason: string
}

export interface AskResponse {
  message: ChatMessage
  evidence: EvidenceItem[]
  route: RouteInfo
  usage: UsageInfo
  insufficient: boolean
  /** 拒答建议：answer() 的每个返回分支都带这个键，无建议时为空数组 */
  suggestions: SuggestionItem[]
  /** 拒答原因：answer() 的每个返回分支都带这个键，非拒答时为空串 */
  refusal_reason: string
  /** 后端扩展：本次被拒绝的知识库（无权限/已删除） */
  denied?: DeniedKb[]
  citations?: { cited: number[]; invalid: number[]; citation_ok: boolean }
}

export interface SearchPreviewResponse {
  evidence: EvidenceItem[]
  routes: RouteInfo
  stats: RouteStats
  /** 后端扩展：本次被拒绝的知识库 */
  denied?: DeniedKb[]
  /** 后端扩展：拼装后的上下文预览（受 CONTEXT_CHAR_BUDGET 限制） */
  context_preview?: string
}

/** search/preview 的原始返回：路由是数组，不是 {kb_routes} */
export interface SearchPreviewRaw {
  evidence: EvidenceItem[]
  routes: KbRoute[]
  stats: RouteStats
  denied?: DeniedKb[]
  context_preview?: string
}

export interface Provider {
  id: number
  name: string
  base_url: string
  api_key_masked: string
  has_key: boolean
  enabled: boolean
  /** 后端可能对老库回填空值，这里按可选处理，读的时候用默认值兜底 */
  protocol?: ProviderProtocol
  protocol_label?: string
  created_at: string | null
}

export interface ModelConfig {
  id: number
  provider_id: number
  provider_name: string
  purpose: ModelPurpose
  purpose_label?: string
  model: string
  label: string
  params: Record<string, unknown>
  is_default: boolean
  enabled: boolean
  embed_dim: number
  embed_signature: string
  last_test_ok: boolean | null
  last_test_at: string | null
  last_test_message: string
}

export interface ModelTestResult {
  ok: boolean
  message: string
  latency_ms: number
}

/** 探测到的一个模型。purpose_hint 只是预选，用户可在导入弹窗里改。 */
export interface DiscoveredModel {
  model: string
  label: string
  purpose_hint: ModelPurpose
  already_imported?: boolean
}

export interface DiscoverModelsResult {
  provider_id: number
  protocol: ProviderProtocol
  items: DiscoveredModel[]
  /** api = 真实探测到；builtin = 供应商不支持 /models，回退内置清单 */
  source: 'api' | 'builtin'
  /** 探测失败原因（中文）；成功时为空串。失败时 items 可能为空或为内置清单 */
  error: string
  total: number
}

export interface ModelImportItem {
  model: string
  purpose: ModelPurpose
  label?: string
}

export interface ModelImportResult {
  created: Array<{ id: number; model: string; purpose: ModelPurpose; label: string }>
  skipped: Array<{ model: string; purpose: ModelPurpose; reason: string }>
  created_count: number
  skipped_count: number
}

export interface EmbeddingKbStatus {
  kb_id: number
  kb_name: string
  embed_signature: string
  embed_dim: number
  consistent: boolean
  chunk_count?: number
  mode?: KbMode
}

export interface EmbeddingActive {
  model_config_id: number | null
  provider: string
  model: string
  signature: string
  dim: number
  offline: boolean
}

export interface EmbeddingStatus {
  /** 归一化后的当前模型信息（api 层从 active 拍平） */
  current_model_config_id?: number | null
  current_model?: string
  current_label?: string
  current_provider?: string
  current_signature?: string
  current_embed_dim?: number
  active?: EmbeddingActive
  consistent?: boolean
  kbs?: EmbeddingKbStatus[]
  rebuilding?: boolean
  has_index?: boolean
  require_confirm?: boolean
  task_id?: number | null
  message?: string
}

export interface EmbeddingSwitchResult {
  ok?: boolean
  need_confirm?: boolean
  model_config_id?: number
  rebuild_required?: boolean
  tasks?: Array<{ kb_id: number; kb_name: string; task_id: number }>
  message?: string
  task_id?: number | null
}

export interface AuditEntry {
  id: number
  username: string
  action: string
  target: string
  detail: string
  created_at: string | null
}

/** 审计日志中文动作名；未知动作回退原文。 */
export const AUDIT_ACTION_LABEL: Record<string, string> = {
  login: '登录',
  logout: '退出登录',
  change_password: '修改密码',
  create_user: '新建用户',
  update_user: '修改用户',
  reset_password: '重置密码',
  set_grants: '调整授权',
  disable_user: '停用账号',
  create_kb: '新建知识库',
  update_kb: '修改知识库',
  delete_kb: '删除知识库',
  upload_documents: '上传文档',
  upload_version: '上传新版本',
  delete_document: '删除文档',
  download: '下载文档',
  edit_wiki: '校对 Wiki',
  publish_wiki: '发布 Wiki',
  unlock_wiki: '解除 Wiki 锁定',
  rebuild: '重建索引',
  create_provider: '新建供应商',
  update_provider: '修改供应商',
  delete_provider: '删除供应商',
  create_model: '新建模型',
  update_model: '修改模型',
  delete_model: '删除模型',
  test_model: '测试模型',
  discover_models: '探测模型',
  import_models: '导入模型',
  switch_embedding: '切换 Embedding',
  ask: '问答',
}

// ------------------------------------------------------------------ 请求体

export interface CreateUserPayload {
  username: string
  display_name: string
  password: string
  role: Role
  grants: Array<{ kb_id: number; level: GrantLevel }>
}

export interface UpdateUserPayload {
  display_name?: string
  role?: Role
  status?: UserStatus
}

export interface ProviderPayload {
  name: string
  base_url: string
  api_key?: string
  enabled: boolean
  protocol?: ProviderProtocol
}

export interface ModelPayload {
  provider_id: number
  purpose: ModelPurpose
  model: string
  label: string
  params: Record<string, unknown>
  is_default: boolean
  enabled: boolean
  embed_dim: number
}

/** 自动更新状态（主进程 updater.cjs 推送） */
export type UpdateStatus =
  | 'idle'
  | 'disabled'
  | 'checking'
  | 'available'
  | 'none'
  | 'downloading'
  | 'ready'
  | 'error'

export interface UpdateState {
  status: UpdateStatus
  currentVersion: string
  latestVersion?: string
  percent?: number
  message?: string
  feed?: string
  checkedAt?: number
}

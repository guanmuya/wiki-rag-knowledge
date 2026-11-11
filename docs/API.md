# API 契约 v1（前后端共同遵守）

> 本文的路由与字段名以 `backend/app/routers/*.py` 的装饰器为事实源；
> 版本、表数、并发、锁超时、测试基线等全局事实口径见 [docs/FACTS.md](FACTS.md) §1，
> 代码引用遵循该文件 §3 的「文件 + 符号名 / 章节」约定。

- 前缀：`/api`
- 认证：`Authorization: Bearer <token>`。免登录的只有 `POST /api/auth/login`、`GET /api/meta`、`GET /api/health` 与 `/api/setup/*`（首次初始化必须在无账号时可用）。
- 错误：`{"detail": "中文错误信息"}`，HTTP 400/401/403/404/409/422。
- 时间：业务接口返回的 ISO 8601 时间统一为 **UTC 且带 `+00:00` 偏移**（前端负责换算本地时区展示；不带偏移的字符串会被浏览器当成本地时间；`/api/diagnostics` 的运维输出不在此列）。**真正分页的只有两个接口**：`GET /api/documents/{id}/chunks`（`?page=1&page_size=20`）与 `GET /api/settings/audit`（`?page=1&page_size=50`），返回 `{"items":[],"total":0,"page":1,"page_size":20}`；其余列表接口只返回 `{"items":[],"total":N}`，没有 `page`/`page_size` 字段（前端一次拿全量）。
- 角色：`admin` | `employee`。知识库授权等级：`read` | `manage`（admin 隐式拥有全部知识库 manage）。

## 0. 首次初始化（免登录）

桌面版没有运维人员，空库首次启动不自动建号，而是由用户在引导页（前端 `#/setup`）自己设账号密码。
后端只在「库里没有任何管理员」时接受 `POST /setup/admin`，之后该接口返回 409。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/setup/status` | `{initialized, org_name, app_name, version}` |
| POST | `/api/setup/admin` | `{username, password, display_name?, org_name?}` → `{ok, user_id, username}`；空库才可调用 |
| GET | `/api/setup/org` | `{org_name}`，侧边栏标题用 |

> 企业名只在创建管理员时写入 `app_settings.org_name` 一次；当前没有单独的修改接口。

## 1. 认证

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/login` | `{username,password}` → `{token, user}` |
| GET | `/api/auth/me` | → `{user, permissions:{is_admin, kb_grants:[{kb_id,level}]}}` |
| POST | `/api/auth/change-password` | `{old_password,new_password}` → `{ok:true}` |
| POST | `/api/auth/logout` | → `{ok:true}`（服务端递增令牌版本，前端丢弃的旧 token 立即失效） |

`user` 结构：`{id, username, display_name, role, status, must_change_password, created_at, last_login_at}`

**令牌撤销（P1-6）**：令牌载荷带 `ver`，即签发时该用户的令牌版本。
`POST /api/auth/change-password`、管理员 `POST /api/users/{id}/reset-password`、
`POST /api/auth/logout` 都会递增该用户的版本，使其**此前签发的全部令牌立即失效**——
之后用旧 token 调任意需鉴权接口均返回 401，须重新登录取新令牌。
因此上述三类操作成功后，前端应丢弃本地令牌；角色与知识库授权仍每次实时查库判定。

## 2. 元信息

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/meta` | 无需登录。`{app_name, version, initialized, needs_setup, org_name, offline_models, capabilities:{llm_configured, embedding_configured, worker_enabled}}`；`needs_setup=true` 时前端跳引导页 |

## 3. 用户管理（admin）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/users` | 列表，含 `grants:[{kb_id,kb_name,level}]` |
| POST | `/api/users` | `{username,display_name,password,role,grants:[{kb_id,level}]}` |
| PATCH | `/api/users/{id}` | `{display_name?,role?,status?}`；至少保留一个可用管理员 |
| PUT | `/api/users/{id}/grants` | `{grants:[{kb_id,level}]}` 全量替换 |
| POST | `/api/users/{id}/reset-password` | `{new_password}`，置 `must_change_password=true` |
| DELETE | `/api/users/{id}` | 软删除=停用，返回 `{ok:true}` |

## 4. 知识库

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/kbs` | 当前用户可见的知识库（admin 全部）。元素 = `serializers.kb_out()`（`serializers.py:85`），含 `my_level`；**列表不带 `index`** |
| POST | `/api/kbs` | admin。`{name,description,mode}` mode=`basic`\|`wiki` |
| GET | `/api/kbs/{id}` | 详情 = `kb_out()` + `index`（`kbs.py:67` 单独追加 `_index_status()`，`kbs.py:122`） |
| PATCH | `/api/kbs/{id}` | admin。改名/描述/切换 mode；切到 wiki 时，没有 Wiki 页的知识库会在已有或正在入库的文档就绪后自动生成首次页面，已有页面可按需手动重建。 |
| DELETE | `/api/kbs/{id}` | admin |
| POST | `/api/kbs/{id}/rebuild` | manage。`{kind:"chunk"\|"wiki"}` → `{task_id}` |
| GET | `/api/kbs/{id}/grants` | admin。`[{user_id,username,display_name,level}]` |
| GET | `/api/kbs/{id}/cost` | 累计整理成本（模型调用/token/耗时，按阶段分组）。需 `read` 权限，详见 7.1 |

知识库结构（`serializers.py:85` 的 `kb_out()`，列表与详情共用同一份）：
```json
{
  "id": 1, "name": "员工手册", "description": "", "mode": "basic",
  "mode_label": "基础分块", "status": "ready", "status_label": "可用",
  "status_detail": "3 个文档 / 42 个索引单元",
  "embed_signature": "openai:text-embedding-3-small:1536", "embed_dim": 1536,
  "wiki_ready": false, "my_level": "manage",
  "created_at": "...", "wiki_ready_at": null,
  "stats": {"doc_count": 3, "chunk_count": 42, "wiki_page_count": 0, "wiki_published_count": 0}
}
```
`status` ∈ `empty|indexing|ready|partial|failed`。

> **`index` 只有详情有**。`GET /api/kbs` 的每个元素就是上面这份结构；`GET /api/kbs/{id}` 在此基础上由 `kbs.py:67` 追加 `index`。前端在列表页读 `index` 只会拿到 `undefined`。

`GET /api/kbs/{id}` 额外的 `index`（`_index_status()`，`kbs.py:122-151`）：
```json
{
  "chunk_ready": true, "chunk_units": 42,
  "wiki_ready": false, "wiki_units": 0, "wiki_current_units": 0,
  "embed_signature": "openai:text-embedding-3-small:1536", "embed_dim": 1536,
  "active_embed_signature": "openai:text-embedding-3-small:1536", "active_embed_dim": 1536,
  "signature_mismatch": false, "pending_tasks": 0
}
```
`index.wiki_ready` 与顶层 `wiki_ready` 同源（`serializers.wiki_ready()`，`serializers.py:58`）；`wiki_units` 与 `wiki_current_units` 均只统计可见、非草稿页面当前版本的章节。`signature_mismatch` / `active_embed_*` 用于判断「换 Embedding 后该库是否需要重建」，`pending_tasks` 是该库排队中与进行中的任务数。

## 5. 文档与任务

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/kbs/{id}/documents` | 文档列表（含当前版本状态） |
| POST | `/api/kbs/{id}/documents` | multipart `file`（可多文件：重复 `files`）→ `{items:[{doc_id,task_id}], errors:[]}` |
| GET | `/api/documents/{id}` | 详情 + `versions:[{id,version_no,status,status_detail,chunk_count,text_chars,created_at}]` |
| POST | `/api/documents/{id}/versions` | multipart `file` 上传新版本 → `{version_id,task_id}` |
| GET | `/api/documents/{id}/download?version_id=` | 原文件下载（后端校验权限） |
| GET | `/api/documents/{id}/chunks?page=` | 分块预览（含 locator/heading/向量维度） |
| POST | `/api/documents/{id}/reindex` | manage。重试这份文档的入库（解析 → 分块 → 索引）：找到该文档最新版本对应的那条入库任务**原地重排**，没有任务记录（历史数据）才补建一条 → `{task_id, version_id, reused, retry_count}`。同一份文档始终只有一条任务记录，点多少次都不会堆积；还没有任何版本时 400（`routers/kbs.py` 的 `reindex_document()`） |
| DELETE | `/api/documents/{id}` | 删除文档及其分块；Wiki 库触发重建任务。切换模式触发的首次 Wiki 生成排队或运行期间返回 409，待任务结束后可重试。 |
| GET | `/api/tasks?status=&kb_id=&include_running=` | 任务列表（轮询用）；`include_running=true` 额外包含符合筛选条件的运行中任务 |
| GET | `/api/tasks/{id}` | 单任务详情 |
| POST | `/api/tasks/{id}/stop-wiki?run_token=` | 需知识库 manage 权限；传任务响应中的 `run_token` 以停止对应执行 → `{ok:true}` |
| POST | `/api/tasks/{id}/retry` | 重试失败任务或已停止的 Wiki 重建 → `{task_id}` |

任务结构：
```json
{"id": 12, "kind": "ingest_document", "kind_label": "文档入库", "status": "running",
 "stage": "向量化", "progress": 55, "kb_id": 1, "kb_name": "员工手册",
 "doc_id": 3, "doc_name": "手册.pdf", "error": "", "attempts": 0,
 "created_at": "...", "started_at": "...", "finished_at": null,
 "result": {"chunks": 42}}
```
`status` ∈ `queued|running|success|partial|failed|stopped`。任务另有 `can_stop_wiki` 和 `wiki_stop_requested`：前者表示当前可请求停止 Wiki 阶段，后者表示停止请求已受理但尚未完成。运行中的任务会在当前阻塞调用完成后停止；未发布的页面会丢弃，已完成的分块及完整 Wiki 页面保留，检索会回退到分块索引直至 Wiki 完整重建。停止仅作用于该任务，其他任务仍可能生成 Wiki。文档入库和分块重建的已停止任务不可原地重试，以免重做已完成的索引；可在知识库设置中发起 Wiki 重建。

## 6. Wiki

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/kbs/{id}/wiki/tree` | 目录：`{pages:[{id,slug,title,summary,status,human_locked,order_index,section_count}]}` |
| GET | `/api/kbs/{id}/wiki/pages/{slug}` | 页面详情：`{page, version:{id,version_no,content_md,origin}, evidence:[...], related:[...]}` |
| PUT | `/api/kbs/{id}/wiki/pages/{slug}` | manage。`{content_md,summary?,note?}` 保存人工版本 → 重建该页索引 |
| GET | `/api/kbs/{id}/wiki/pages/{slug}/versions` | 版本历史 |
| POST | `/api/kbs/{id}/wiki/pages/{slug}/publish` | 发布草稿 |
| POST | `/api/kbs/{id}/wiki/pages/{slug}/unlock` | 解除人工锁定（允许下次自动覆盖） |

证据结构：`{chunk_id,doc_id,doc_name,doc_version_id,version_no,locator,quote}`

删除文档后，当前版本的证据中只要有一条指向该文档，页面即从普通用户的目录、详情、关联页、检索和建议问题中隐藏（包括有其他有效来源的页面）；普通用户访问详情或修改接口返回 404。历史版本按各自的证据过滤。系统管理员仍可查看保留的页面和证据，此时响应的 `status` 为 `needs_review`、`status_label` 为「来源已删除，待复核」。人工编辑、发布或解锁不会解除隐藏；以有效来源重新生成当前版本后才恢复普通可见性。此规则依赖已有证据记录，无法识别历史版本中未记录的来源；已删除的原始文档文件不会因此保留。

## 7. 会话与问答

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/conversations` | 我的会话列表（置顶优先，其次按最近更新；条目含 `pinned`、`message_count`） |
| POST | `/api/conversations` | `{title,kb_ids}` |
| GET | `/api/conversations/{id}` | 含 `messages:[{id,role,content,kb_ids,evidence,route,latency_ms,status,created_at,insufficient,usage,suggestions,refusal_reason}]`（`serializers.message_out()`） |
| PATCH | `/api/conversations/{id}` | 改标题 / 换知识库选择 / 置顶（`pinned: true/false`） |
| DELETE | `/api/conversations/{id}` | 删除 |
| POST | `/api/conversations/{id}/ask` | `{question,kb_ids?,top_k?,model_config_id?}` → `{content, message, evidence, route, usage, insufficient, citations, suggestions, refusal_reason, denied}` |
| POST | `/api/conversations/{id}/ask/stream` | 同上请求体；SSE 流式推送思考步骤、答案增量与最终结果（见 7.1） |
| GET | `/api/chat/models` | 问答页模型下拉：`{items:[{id,model,label,provider,is_default,display}],default_id}`；对所有登录用户开放，只列启用中的对话模型 |
| POST | `/api/search/preview` | `{kb_ids,query}` → `{evidence, routes, stats, denied, context_preview, usage}`（不生成回答，用于调参/实验） |

`model_config_id` 可选：指定本轮问答使用的对话模型（`cfg_override`），不传则沿用模型设置里解析出的默认模型；传了未启用、非 `llm` 用途或供应商已停用的 id 会先于落库返回 400。`default_id` 用与问答相同的 `llm.resolve_model()` 解析。界面在发送默认模型提问前刷新默认值，并将该模型固定到本轮。显式指定模型时，生成模型错误只返回类别与状态码；查询向量化错误同样只返回类别与状态码，避免暴露供应商地址或凭据。

`ask` 返回：
```json
{
  "content": "一线城市住宿上限 600 元每晚[1]。",
  "message": {"id": 88, "role": "assistant", "content": "...", "created_at": "..."},
  "evidence": [{"evidence_id":"chunk:12","kb_id":1,"kb_name":"员工手册","source_type":"chunk",
                "text":"...","score":0.03,"rank":1,"doc_name":"手册.pdf","version_no":1,
                "locator":"第 3 页","heading":"请假","page_title":null,"page_slug":null,
                "channels":{"rank_0":1,"vector_sim":0.81},
                "evidence":[{"doc_name":"手册.pdf","locator":"第 3 页","quote":"..."}]}],
  "route": {"kb_routes":[{"kb_id":1,"kb_name":"...","mode":"wiki","used":"wiki",
             "reason":"Wiki 已就绪，检索 Wiki 章节","fallback":false,
             "keyword_hits":4,"vector_hits":6,"wiki_hits":6}],
            "stats":{"candidate_total":14,"final_total":8,"embedding_ok":true,"rrf_k":60}},
  "usage": {"calls":3,"chat_calls":1,"embed_calls":2,"vision_calls":0,
            "live_calls":3,"offline_calls":0,
            "prompt_tokens":1200,"completion_tokens":300,
            "embed_tokens":48,"embed_texts":2,"total_tokens":1548,
            "latency_ms":2400,"latency_chat_ms":2200,"latency_embed_ms":200,
            "mode":"live","embed_mode":"live","degraded":false,"notes":[]},
  "insufficient": false,
  "citations": {"cited": [1], "invalid": [], "citation_ok": true},
  "suggestions": [],
  "refusal_reason": "",
  "denied": []
}
```

拒答时（`insufficient=true`）：`content` 以固定前置句 `现有资料不足以回答这个问题。` 开头，
正文带「本次检索到这些资料」与「你可以试试这样问」；`suggestions` 是结构化建议，
每项 `{kb_name, kind, title, question?}`，`kind` ∈ `resource`（本次命中资料）| `question`（可点击填入输入框）；
`refusal_reason` 给出判定原因（如「回答开头即声明资料不足」）。
历史消息与当次回答同口径：`chat.ask()` 把 `insufficient` / `usage` / `suggestions` / `refusal_reason` 一并写进 `route` 快照（`chat.py:154-161`），`serializers.message_out()` 再从 `route` 读回并展开成顶层字段（`serializers.py:237-242`）；老消息没有这两个键、或数据损坏时退回 `[]` / `""`（`serializers.py:225-230`），不会让一条脏消息把整个会话接口打挂。

**问答是「先落库、再生成」的两段写**（`chat.py`）：`POST .../ask` 先写入提问与一条 `status="pending"` 的占位回答并提交，生成结束后把占位更新成 `status="ok"`。所以生成过程中 `GET /conversations/{id}` 就能读到「提问 + 正在生成」——用户离开页面再回来（甚至刷新）不会看到一个被清空的会话；前端在 `pending` 期间每 2 秒轮询一次，读到 `ok` 即停止（`ChatPage.tsx`）。生成失败时占位写成 `status="failed"`，`content` 与 `route.error` 均返回通用提示；普通接口保留 HTTP 状态码，流式接口的 `error.detail` 同样使用通用提示。模型降级原因只返回安全的错误类别和状态码，不返回供应商异常原文。旧历史消息读取时也会处理这些诊断字段，但不会改写已存数据库。进程重启时启动流程会把遗留的 `pending` 统一收尾成 `failed`，同一会话来了新提问也会把上一条未完成的回答收尾（`chat.recover_pending_messages()` / `chat._close_pending()`）。

### 7.1 检索链路与流式思考过程

`/ask` 与 `/ask/stream` 共用同一条链路（`backend/app/services/qa_pipeline.py`）：

```
意图分析 → 闲聊？→ 通用回复（不检索）
        └→ 查询路由（original / rewrite / multi_query）
              → 检索（每条 Query：关键词 + 向量 + RRF；多查询再融合去重）
              → 覆盖度评估（sufficient / missing_queries）
              → 不足且未到上限：只重写 missing_queries，回到检索（最多 2 次）
              → 回答生成（原始问题 + Query 列表 + 参考资料）
```

链路快照写在 `route.pipeline`，随消息落库，刷新后可从历史还原：

```json
{
  "mode": "rag",
  "intent": {"label": "规范查询", "needs_retrieval": true, "reason": "...", "source": "model"},
  "route": {"mode": "multi_query", "queries": ["...", "..."], "reason": "...", "source": "model"},
  "rounds": [{"round": 1, "queries": ["..."], "hits": {"...": 5},
              "evidence_total": 10,
              "evaluation": {"sufficient": false, "missing_queries": ["..."],
                             "reason": "...", "source": "model"}}],
  "retries": 1,
  "evaluation": {"sufficient": true, "missing_queries": [], "reason": "...", "source": "model"},
  "steps": [{"id": "intent", "title": "理解问题", "status": "done", "detail": "..."}]
}
```

`/ask/stream` 返回 `text/event-stream`，每个事件都是 `data: {json}`：

| `type` | 字段 | 说明 |
|---|---|---|
| `step` | `id,title,status,detail` | 思考步骤开始（`running`）/结束（`done`）；前端按 `id` 原位更新 |
| `answer` | `delta` | 回答文本增量（OpenAI 兼容协议原生流式；其它协议整段产出） |
| `answer_replace` | `content` | 最终正文被替换（拒答兜底 / 部分回答补「未找到」说明） |
| `done` | 与 `/ask` 返回同形 | 最终结果；同时已把消息落库 |
| `error` | `message,detail` | 生成失败；占位消息已按 `failed` 收尾 |

前端用 `fetch` + `ReadableStream` 读取（`api.ts` 的 `chatApi.askStream`），因为 `EventSource`
不能携带 `Authorization` 头，也不支持 POST body。链路分析节点没有可用模型时退回规则判断，
回答生成没有可用模型时退回抽取式回答；离线模式下事件流与步骤展示仍然完整。

#### `top_k` 的两个语义（同一个参数）

`top_k` 同时被当作**每库候选数**和**最终条数上限**，两处口径不同（`retrieval.py:596`、`retrieval.py:665`）：

| 位置 | 表达式 | 不传时 | 传 `top_k=1` 时 |
|---|---|---|---|
| 每库候选数 | `per_kb = top_k or PER_KB_CANDIDATES` | 8 | 1 |
| 最终条数上限 | `total = MAX_EVIDENCE if top_k is None else max(top_k, 3)` | 10 | 3 |

所以 **`top_k=1` 不会只返回 1 条**——最终名额被 `max(top_k, 3)` 抬到 3。但实际条数还受候选池限制：
单个知识库、每库只取 1 条候选时，候选池里就只有 1 条，最终也只返回 1 条。要真的凑满 3 条，
候选池至少要有 3 条：要么 `top_k >= 3`（单库也能凑满），要么有多个库、每库各出 1 条以上。

`top_k` 在 `schemas.AskIn` 里没有下限校验（`schemas.py:125`），传 `0` 会因 `or` 短路回落到每库默认 8 条、
最终名额仍是 3；负数不要传。`POST /api/search/preview` 的请求体里**没有** `top_k`
（`schemas.SearchPreviewIn`），固定按 `MAX_EVIDENCE = 10` 走，要算 Hit@1 只能从返回结果里自己截第一条。

`usage` 字段说明（**只加字段，不删字段**，老前端按 `calls`/`prompt_tokens` 读仍然正确）：

| 字段 | 含义 |
|---|---|
| `calls` | 总调用次数 = `chat_calls + embed_calls + vision_calls`（含离线降级） |
| `chat_calls` / `embed_calls` / `vision_calls` | 分通道调用次数。**生成与向量必须分列**：计费口径不同，混在一起算不出成本 |
| `live_calls` / `offline_calls` | 真实供应商调用（可计费）/ 离线降级（本地哈希、抽取式，不计费） |
| `prompt_tokens` / `completion_tokens` | 生成侧输入 / 输出 token |
| `embed_tokens` | 向量侧 token，**不并入** `prompt_tokens`（单价不同） |
| `embed_texts` | 被向量化的文本条数（离线模式也记，用于衡量索引工作量） |
| `total_tokens` | 全通道合计 = `prompt_tokens + completion_tokens + embed_tokens` |
| `latency_ms` | 总耗时；`latency_chat_ms` / `latency_embed_ms` 为分通道耗时 |
| `mode` | **生成通道**模式（`live`/`offline`）；前端据此显示「未调用生成模型」 |
| `offline_reason` | 生成通道的安全降级类别（如供应商 HTTP 状态码）；不会包含供应商响应正文或地址 |
| `embed_mode` | **向量通道**模式。单独存在，避免「只有向量走离线」被误报成整体离线 |
| `degraded` | 任一通道降级 |

> 修复记录：`retrieval.py` 查询向量化、`wiki._index_sections()` Wiki 章节向量化、
> `routers/wiki.py` 人工校对后重建索引，这三处曾漏传 `stats`，导致成本被系统性低估
> （尤其是 Wiki 库，其成本大头正是章节向量化）。现已全部计入。

`search/preview` 的响应（`chat.py:182-199`，只有检索、无生成）：

```json
{
  "evidence": [{"evidence_id":"chunk:12","...":"与 ask 的 evidence[] 同一结构，另含 coverage"}],
  "routes": [{"kb_id":1,"kb_name":"员工手册","mode":"wiki","status":"ready","used":"wiki",
              "reason":"Wiki 已就绪，检索 Wiki 章节","keyword_hits":4,"vector_hits":6,
              "wiki_hits":6,"fallback":false}],
  "stats": {"query_terms":["差旅","报销"],"embedding_ok":true,"embedding_error":"",
            "per_kb_limit":8,"candidate_total":14,"final_total":8,"rrf_k":60,
            "kb_coverage":{"员工手册":8}},
  "denied": [],
  "context_preview": "[1] 知识库《员工手册》 文档《手册.pdf》· 版本 v1 · 位置：第 3 页 · 小节：请假\n…",
  "usage": {"calls":2,"chat_calls":0,"embed_calls":2,"...":"同 ask 的 usage"}
}
```

- `routes` 是**扁平数组**（`KbRoute.to_dict()`，`retrieval.py:371`），不是 `ask` 的 `route.kb_routes`；`stats` 也没有 `ask` 那层 `route` 包装。
- `denied` 与 `ask` 同构：`[{kb_id, kb_name, reason}]`，列出本次请求里被权限拦下的库（`chat.py:184` 计算、`chat.py:195` 返回）。
- `context_preview` 是 `retrieval.build_context(evidence)` 的原文（`chat.py:196`），即真正拼进提示词的那段文字，受 `CONTEXT_CHAR_BUDGET = 9000` 字符预算约束（`retrieval.py:701`）。
- `usage` 与 `ask` 同结构：`chat_calls == 0`，查询向量化仍会记 `embed_calls` / `embed_tokens`。

## 7.1 知识库累计成本

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/kbs/{id}/cost` | 该库累计整理成本。需该库 `read` 及以上权限，无权限 403 |

```json
{
  "kb_id": 1, "kb_name": "人事制度库", "mode": "wiki", "mode_label": "Wiki 增强",
  "my_level": "read",
  "totals": {"calls": 46, "chat_calls": 14, "embed_calls": 32,
             "live_calls": 46, "offline_calls": 0,
             "prompt_tokens": 18200, "completion_tokens": 4300, "embed_tokens": 9800,
             "embed_texts": 32, "total_tokens": 32300, "latency_ms": 61000,
             "record_count": 5},
  "by_group": {
    "ingest": {"calls": 20, "embed_calls": 20, "...": "..."},
    "wiki":   {"calls": 26, "chat_calls": 14, "embed_calls": 12, "...": "..."}
  },
  "group_labels": {"ingest": "文档入库", "wiki": "Wiki 生成", "other": "其他"},
  "by_stage": [{"stage": "rebuild_wiki", "label": "重建 Wiki", "calls": 26, "...": "..."}],
  "by_mode": {"live": {"calls": 46, "...": "..."}, "offline": {"calls": 0, "...": "..."}},
  "legacy_records": 0,
  "records": [{"id": 5, "kind": "wiki_edit", "kind_label": "人工校对重建索引",
               "calls": 3, "embed_calls": 3, "total_tokens": 120, "...": "..."}]
}
```

- `by_group` 是论文对比的主指标：`ingest`（分块入库）vs `wiki`（Wiki 生成）。
- `by_mode` 用于剔除离线演示的假开销：只报 `live` 才是真实费用。
- `legacy_records`：升级前的老任务没有成本流水，从 `Task.result["usage"]` 回读的条数。

## 7.2 已记录用量概览（admin）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/usage?days=` | admin。`days` ∈ `7` \| `30`（默认）\| `0`（全部），其他值 422 → `{period_days, timezone, totals, daily, by_source}`（`routers/usage.py` 的 `get_usage()`，汇总口径见本节下文） |

`GET /api/usage?days=30` 汇总全局已记录用量，仅管理员可访问。`days` 可为 `7`、`30`（默认）或 `0`（全部），其他值返回 422。7/30 天以 UTC 当日为末日，按 UTC 日期补齐无记录的日期。

响应为 `{period_days, timezone, totals, daily, by_source}`；`timezone` 为 `UTC`，`daily` 是带 `date` 的每日汇总，`by_source` 的 `source` 为 `processing`（知识库处理）或 `chat`（问答）。各汇总包含 `calls`、`live_calls`、`offline_calls`、`chat_calls`、`embed_calls`、`vision_calls`、`prompt_tokens`、`completion_tokens`、`embed_tokens`、`total_tokens`、`latency_ms` 和 `record_count`。`total_tokens` 为前三类 Token 之和。

知识库处理使用成本流水；缺少流水的旧任务回退到任务结果，同一任务已有流水时不会重复计数。问答使用成功保存的助手消息，每条消息只计一次。处理按流水入账时间统计，旧任务按完成时间（无完成时间则创建时间），问答按消息创建时间统计。删除知识库或会话会同时删除其关联记录。

该接口不包括检索调试、连接测试、未保存或失败的问答；供应商可能不返回 Token 数，失败或中断的调用也可能产生未记录用量。历史流水没有供应商、模型、价格或余额信息，响应不能作为账单或完整用量。

## 8. 模型设置（admin）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/settings/providers` | 供应商列表，`api_key_masked` 脱敏 |
| POST | `/api/settings/providers` | `{name,base_url,api_key,enabled}` |
| PATCH | `/api/settings/providers/{id}` | `api_key` 传空=不改，传 `"__clear__"`=清空 |
| POST | `/api/settings/providers/{id}/discover` | 探测供应商侧模型列表；**永远返回 200 + 结果对象** `{provider_id, protocol, items[], source, error, total}`，`items[]` 含 `already_imported`；探测失败是正常业务状态（`routers/settings.py` 的 `discover_provider_models()`） |
| POST | `/api/settings/providers/{id}/models/import` | `{items:[{model,purpose,label}]}` 批量导入；按 `(provider, purpose, model)` 幂等，已存在跳过 → `{created[], skipped[], created_count, skipped_count}`（`routers/settings.py` 的 `import_provider_models()`） |
| DELETE | `/api/settings/providers/{id}` | 同时删除其模型 |
| GET | `/api/settings/models` | 模型列表，含 `provider_name`、`last_test_*` |
| POST | `/api/settings/models` | `{provider_id,purpose,model,label,params,is_default,enabled,embed_dim}` |
| PATCH | `/api/settings/models/{id}` | 同上字段可选 |
| DELETE | `/api/settings/models/{id}` | |
| POST | `/api/settings/models/{id}/test` | 连接测试 → `{ok,message,latency_ms}` |
| POST | `/api/settings/embedding/switch` | `{model_config_id,confirm}`：已有索引时需 `confirm=true`，触发全量重建任务 |
| GET | `/api/settings/embedding/status` | 当前 Embedding、各库签名是否一致、是否有重建进行中 |
| GET | `/api/settings/audit?page=` | 审计日志 |

## 8.1 个人 Agent（admin）

v1 全部要求管理员令牌；CLI 核心以应用进程身份执行本机命令。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/agent/cores` | `{items, errors, plugin_dir, schema, supported_protocols}`；每项含 `installed/version/enabled/available/unavailable_reason/protocols/compatible_model_count` |
| GET | `/api/agent/models?core_id=` | `{items, core_id}`；每项含 `protocol/compatible/display`，`compatible=false` 表示协议不匹配 |
| POST | `/api/agent/plugins/rescan` | 清探测缓存并重新扫描插件目录，返回同 cores |
| PATCH | `/api/agent/plugins/{id}` | `{enabled}`，返回单个核心 |
| GET | `/api/agent/sessions` | 当前管理员的会话列表 |
| POST | `/api/agent/sessions` | `{title,core_id,model_config_id,kb_ids,workspace}`；`core_id` 省略时优先内置知识库核心 |
| GET | `/api/agent/sessions/{id}` | 会话 + `messages[]` + `core` + `running` |
| PATCH | `/api/agent/sessions/{id}` | `{title,core_id,model_config_id,kb_ids,workspace}`；执行中 409；换核心会追加一条 `role=system` 消息 |
| DELETE | `/api/agent/sessions/{id}` | 执行中 409 |
| POST | `/api/agent/sessions/{id}/messages` | `{content}` → `{run_id, user_message, assistant_message, session}`；只建 run，不等执行结束 |
| GET | `/api/agent/runs/{id}/events?after=` | `{events,next,status,error,message?,run_id,session_id}`；`status` 为 `running/ok/failed/stopped`；`run_id` 就是 pending assistant 消息 id |
| POST | `/api/agent/runs/{id}/stop` | 终止子进程树；已结束 409 |

事件 `type`：`status`（阶段文案）、`text`（正文增量）、`log`（CLI stderr/命令摘要）、`evidence`（知识库证据）、`done`、`error`、`stopped`。

## 8.2 运维诊断（admin）

两个接口都由 `backend/app/routers/diagnostics.py` 提供，需管理员令牌；运维输出的时间字段
不受本文开头「时间」一条里「UTC 且带 `+00:00` 偏移」的约定约束。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/diagnostics/lock` | 写锁现场快照：最近阻塞事件、慢语句、正在执行中的语句、全部线程栈；响应另附 `process` 进程信息（`diagnostics.py` 的 `lock_diagnostics()`） |
| GET | `/api/diagnostics/process` | 进程身份：`pid` / `ppid` / 真实端口 / 工作目录 / 数据目录 / Python 版本（`diagnostics.py` 的 `process_diagnostics()`） |

## 9. 前端约定

- 令牌存 `localStorage.wrk_token`；401 时清空并跳登录页。
- `must_change_password=true` 登录后强制进入改密弹窗。
- 改密 / 登出成功后旧 token 已失效，须丢弃本地令牌并重新登录，不能继续带旧 token 调接口。
- 任务进度用 2 秒轮询 `GET /api/tasks?status=queued,running`。
- 问答页左栏会话、顶部知识库多选、右栏证据卡片可折叠查看原文依据。

### 9.1 请求只有一个出口（P1-10）

`frontend/src/api.ts` 的 `send()` 是全站唯一的 `fetch` 出口：注入 `Bearer` 令牌、超时控制、
401 清登录态、403 弹提示、`503` / `database is locked` 翻译成「后端忙」，**只在这一处实现**。
四个调用面都走它，新增接口不再需要重复这套判断：

| 入口 | 用途 | 与 `send()` 的关系 |
| --- | --- | --- |
| `request<T>()` | 普通 JSON 接口 | `send()` + 解析 JSON（204/空体返回 `undefined`） |
| `requestBlob()` | 文件下载 | `send()` + 取 `Blob` |
| `chatApi.askStream()` | SSE 流式问答 | `send()`（`timeoutMs: 0`，由调用方 `signal` 中止）+ 自行解析 SSE |
| `probeBackend()` | 探活（免登录） | `send()` + `silent401/silent403/silentBusy`，失败返回结构化结果而不抛异常 |

失败统一是 `ApiError`，其 `kind`（`http` / `timeout` / `offline` / `busy`）是界面选文案的依据，
界面**不应该**去匹配错误字符串。

### 9.2 错误兜底（P1-10）

| 层级 | 组件 | 接住什么 |
| --- | --- | --- |
| 接口层 | `ApiError` + 各页 `ErrorState` | 请求失败、超时、后端忙、离线：可重试，不清空已有数据 |
| 页面层 | `Layout` 里的 `ErrorBoundary`（`key=pathname`） | 单个页面**渲染期**抛错：只在内容区显示兜底页，侧边栏仍在，换页面即自动复位 |
| 应用层 | `main.tsx` 根部的 `ErrorBoundary` | `BackendGate`、`AuthProvider` 等更外层抛错：保证任何情况下不白屏 |

React 错误边界只接渲染期异常，不接事件处理器 / 异步回调 / SSE 里的错误——那些仍由上面第一层处理，
两者互补。兜底页给「重试 / 回到首页 / 重新加载应用」三个出口，并把堆栈折叠在「查看详情」里。

## 10. 契约同步与校验（P1-5）

前后端跨语言、又都手写类型，字段一改就容易两边对不上。本节规定**唯一的事实源**与**唯一的检查入口**。

### 10.1 事实源

| 契约面 | 事实源 | 前端的对端 |
| --- | --- | --- |
| 路由路径与方法 | `backend/app/routers/*.py` 的 `@router.<method>()` 装饰器 | 本文各节表格 + `frontend/src/api.ts` |
| 请求体字段与枚举 | `backend/app/schemas.py`、`backend/app/routers/setup.py` 的 Pydantic 模型 | `frontend/src/types.ts` 的载荷接口与联合类型 |
| 桌面 IPC 暴露面 | `frontend/electron/preload.cjs` 的 `contextBridge` 键 | `frontend/src/types.ts` 的 `DesktopBridge` |
| 响应体 | `backend/app/serializers.py` 的拼装函数（不是 Pydantic 出参模型） | 见 10.2 为何不参与校验 |

### 10.2 为什么是「校验」而不是「生成」

本项目响应体由 `serializers.py` 拼 dict，FastAPI 生成的 OpenAPI 里这些响应是空壳 `{}`，
据它 codegen 只能产出一堆空类型，反而给假安全感。而真正会在运行时 422 的是**请求体**，
它恰好是规整的 Pydantic 模型。所以做法是：把请求体模型当事实源，与前端手写类型做**比对**，
既能真拦住漂移，又不需要改后端、不引入任何新依赖。

### 10.3 校验器

`frontend/scripts/check-api-contract.mjs`，命令 `pnpm check:contract`（CI 的第 2 项检查，失败即阻断合并）。
只读四组，任一不过退出码 1：

1. **请求体字段**：前端多出后端不认的字段（死字段 / 拼错）→ 报错；后端必填但前端漏写或写成可选 → 报错（会 422）。
2. **枚举取值**：`Role` / `UserStatus` / `KbMode` / `GrantLevel` / `ModelPurpose` / `ProviderProtocol`
   的字面量联合 ↔ 后端对应 `Literal[...]`，两边取值必须完全一致。
3. **路由覆盖**：后端每条路由都要在本文有契约行（路径参数名 `{id}` / `{pid}` 会被抹平后再比对）。
4. **桌面桥**（两层）：`preload.cjs` 暴露的键 ↔ `types.ts` 的 `DesktopBridge`；
   `main.cjs` 里 `ipcMain.handle('wrk:info')` 返回的字段 ↔ `types.ts` 的 `DesktopInfo`。
   双向都不许缺——P1-10 记录的三处漂移（漏 `getFailure`、`openPath` 漏 `'install'`、
   `getInfo` 只声明了 4 个字段而主进程返回 14 个）正是这一层。

新增接口时，除了写代码与本文表格，还要在脚本顶部的 `PAYLOAD_MODELS` / `UNION_LITERALS`
映射表里登记新的一行，否则它不受保护。


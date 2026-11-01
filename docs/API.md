# API 契约 v1（前后端共同遵守）

- 前缀：`/api`
- 认证：`Authorization: Bearer <token>`。免登录的只有 `POST /api/auth/login`、`GET /api/meta`、`GET /api/health` 与 `/api/setup/*`（首次初始化必须在无账号时可用）。
- 错误：`{"detail": "中文错误信息"}`，HTTP 400/401/403/404/409/422。
- 时间：ISO 8601 字符串。**真正分页的只有两个接口**：`GET /api/documents/{id}/chunks`（`?page=1&page_size=20`）与 `GET /api/settings/audit`（`?page=1&page_size=50`），返回 `{"items":[],"total":0,"page":1,"page_size":20}`；其余列表接口只返回 `{"items":[],"total":N}`，没有 `page`/`page_size` 字段（前端一次拿全量）。
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
| POST | `/api/auth/logout` | → `{ok:true}`（无状态令牌，前端丢弃即可） |

`user` 结构：`{id, username, display_name, role, status, must_change_password, created_at, last_login_at}`

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
| PATCH | `/api/kbs/{id}` | admin。改名/描述/切换 mode（切到 wiki 不自动生成，需手动重建） |
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
  "wiki_ready": false, "wiki_units": 0,
  "embed_signature": "openai:text-embedding-3-small:1536", "embed_dim": 1536,
  "active_embed_signature": "openai:text-embedding-3-small:1536", "active_embed_dim": 1536,
  "signature_mismatch": false, "pending_tasks": 0
}
```
`index.wiki_ready` 与顶层 `wiki_ready` 同源（`serializers.wiki_ready()`，`serializers.py:58`）；`signature_mismatch` / `active_embed_*` 用于判断「换 Embedding 后该库是否需要重建」，`pending_tasks` 是该库排队中与进行中的任务数。

## 5. 文档与任务

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/kbs/{id}/documents` | 文档列表（含当前版本状态） |
| POST | `/api/kbs/{id}/documents` | multipart `file`（可多文件：重复 `files`）→ `{items:[{doc_id,task_id}], errors:[]}` |
| GET | `/api/documents/{id}` | 详情 + `versions:[{id,version_no,status,status_detail,chunk_count,text_chars,created_at}]` |
| POST | `/api/documents/{id}/versions` | multipart `file` 上传新版本 → `{version_id,task_id}` |
| GET | `/api/documents/{id}/download?version_id=` | 原文件下载（后端校验权限） |
| GET | `/api/documents/{id}/chunks?page=` | 分块预览（含 locator/heading/向量维度） |
| DELETE | `/api/documents/{id}` | 删除文档及其分块；Wiki 库触发重建任务 |
| GET | `/api/tasks?status=&kb_id=` | 任务列表（轮询用） |
| GET | `/api/tasks/{id}` | 单任务详情 |
| POST | `/api/tasks/{id}/retry` | 失败任务重试 → `{task_id}` |

任务结构：
```json
{"id": 12, "kind": "ingest_document", "kind_label": "文档入库", "status": "running",
 "stage": "向量化", "progress": 55, "kb_id": 1, "kb_name": "员工手册",
 "doc_id": 3, "doc_name": "手册.pdf", "error": "", "attempts": 0,
 "created_at": "...", "started_at": "...", "finished_at": null,
 "result": {"chunks": 42}}
```
`status` ∈ `queued|running|success|partial|failed`。

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

## 7. 会话与问答

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/conversations` | 我的会话列表 |
| POST | `/api/conversations` | `{title,kb_ids}` |
| GET | `/api/conversations/{id}` | 含 `messages:[{id,role,content,kb_ids,evidence,route,latency_ms,created_at,insufficient,usage,suggestions,refusal_reason}]`（`serializers.message_out()`，`serializers.py:212-243`） |
| PATCH | `/api/conversations/{id}` | 改标题 / 换知识库选择 |
| DELETE | `/api/conversations/{id}` | 删除 |
| POST | `/api/conversations/{id}/ask` | `{question,kb_ids?,top_k?}` → `{content, message, evidence, route, usage, insufficient, citations, suggestions, refusal_reason, denied}` |
| POST | `/api/search/preview` | `{kb_ids,query}` → `{evidence, routes, stats, denied, context_preview, usage}`（不生成回答，用于调参/实验） |

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

## 8. 模型设置（admin）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/settings/providers` | 供应商列表，`api_key_masked` 脱敏 |
| POST | `/api/settings/providers` | `{name,base_url,api_key,enabled}` |
| PATCH | `/api/settings/providers/{id}` | `api_key` 传空=不改，传 `"__clear__"`=清空 |
| DELETE | `/api/settings/providers/{id}` | 同时删除其模型 |
| GET | `/api/settings/models` | 模型列表，含 `provider_name`、`last_test_*` |
| POST | `/api/settings/models` | `{provider_id,purpose,model,label,params,is_default,enabled,embed_dim}` |
| PATCH | `/api/settings/models/{id}` | 同上字段可选 |
| DELETE | `/api/settings/models/{id}` | |
| POST | `/api/settings/models/{id}/test` | 连接测试 → `{ok,message,latency_ms}` |
| POST | `/api/settings/embedding/switch` | `{model_config_id,confirm}`：已有索引时需 `confirm=true`，触发全量重建任务 |
| GET | `/api/settings/embedding/status` | 当前 Embedding、各库签名是否一致、是否有重建进行中 |
| GET | `/api/settings/audit?page=` | 审计日志 |

## 9. 前端约定

- 令牌存 `localStorage.wrk_token`；401 时清空并跳登录页。
- `must_change_password=true` 登录后强制进入改密弹窗。
- 任务进度用 2 秒轮询 `GET /api/tasks?status=queued,running`。
- 问答页左栏会话、顶部知识库多选、右栏证据卡片可折叠查看原文依据。

# 企业知识库文档整理与问答系统

面向单个企业的多知识库文档整理与增强问答桌面程序。文档入库后同时产出两种可检索形态：**原文分块索引**（关键词 + 向量）与 **Wiki 增强索引**（模型归纳成页面与章节），问答时按库自动路由、融合召回、强制标注引用编号，并在资料不足时给出结构化的「缺什么 + 可以怎么问」。

后端是 FastAPI + SQLAlchemy + SQLite 的单机进程，前端是 React + Vite，Electron 负责把后端作为 **sidecar 子进程**拉起并动态分配端口。没有外部队列、没有独立数据库服务、不需要运维。

- 应用名 `企业知识库文档整理与问答系统`，版本 `1.0.10`（`backend/app/main.py:31`；`frontend/package.json` 的 `version` 同为 `1.0.10`）
- 安装包产品名 `企业知识库`，appId `com.wikiraq.knowledge`（`frontend/electron-builder.yml`）
- 后端测试基线 **200 passed, 1 skipped**（`backend/tests/`，23 个文件；v1.0.10 新增 `test_freeze_during_task.py` 8 条）

---

## 1. 核心能力

| 能力 | 实现要点 | 代码位置 |
| --- | --- | --- |
| 六类文档解析 | `pdf / docx / md / txt / xlsx / pptx`；图片只记元信息不解析 | `backend/app/services/parser.py:559`、`backend/app/config.py:27` |
| 文本分块 | 目标 700 字符；**标题变化强制切分**，重叠只在同标题内生效 | `backend/app/services/chunking.py:158` |
| 表格分块 | 按字符预算切行组 + 每块重复表头 + `locator` 记录行区间 | `backend/app/services/chunking.py:76` |
| 双形态索引 | `basic` = 分块；`wiki` = 主题归纳 → 页面 → 章节 | `backend/app/models.py:29`、`backend/app/services/wiki.py` |
| Wiki Map-Reduce | 全库按字符预算分片归纳，片内用全库全局编号，合并去重 | `backend/app/services/wiki.py:119-308` |
| 两层融合检索 | 库内**加权 RRF**；跨库**覆盖率 × 名次衰减 × 均衡因子** | `backend/app/services/retrieval.py:264`、`backend/app/services/retrieval.py:583` |
| 逐库自动路由 | `wiki` 未就绪回退分块并标 `fallback`，不静默降级 | `backend/app/services/retrieval.py:557` |
| 证据链 | 回答 `[编号]` → `Message.evidence` → `Chunk.locator` / `WikiEvidence` | `backend/app/services/answering.py:415` |
| 结构化拒答 | 固定前置句 + 本次命中资料 + 可点击建议问题 | `backend/app/services/answering.py:122-177` |
| 成本核算 | `CallStats` 分 chat/embed/vision 三通道，流水落 `cost_records` | `backend/app/services/llm.py:59`、`backend/app/services/cost.py` |
| 三协议模型适配 | `openai` / `openai-responses` / `anthropic` | `backend/app/services/llm.py:4-6`、`backend/app/models.py:321` |
| 首次启动引导 | 无管理员时前端走 `/setup` 建号，不再随机生成密码 | `backend/app/routers/setup.py`、`backend/app/main.py:27` |
| 离线可跑 | 无模型时 LLM 抽取式、Embedding 256 维哈希向量 | `backend/app/services/llm.py:38-39`、`backend/app/services/llm.py:290` |

---

## 2. 架构

```
┌──────────────────────────────────────────────────────────────┐
│ Electron 33 主进程  frontend/electron/main.cjs                │
│  · 窗口 / 菜单 / 日志（userData/logs/main.log）                │
│  · spawn Python sidecar，stdout 读 "WRK_READY {json}" 拿端口   │
│  · stdin EOF / SIGTERM 双保险关停子进程                        │
└───────────────┬──────────────────────────────────────────────┘
                │ ipcMain.handle('wrk:info') → { apiBase }
┌───────────────▼──────────────────────────────────────────────┐
│ 渲染进程  React 18 + Vite 6 + HashRouter                       │
│  /setup /login /kbs /kbs/:id /chat /search /tasks /about       │
│  /admin/users /admin/models                                    │
└───────────────┬──────────────────────────────────────────────┘
                │ HTTP  /api/*（Bearer JWT）
┌───────────────▼──────────────────────────────────────────────┐
│ Python 3.13 sidecar  FastAPI + Uvicorn                        │
│  routers: auth setup kbs wiki chat settings                   │
│  services: parser chunking llm retrieval answering wiki cost   │
│            ingest worker                                       │
│  进程内 Worker 线程 ×2（WORKER_CONCURRENCY=2）轮询 tasks 表     │
└───────────────┬──────────────────────────────────────────────┘
                │
┌───────────────▼──────────────────────────────────────────────┐
│ SQLite 单文件（WAL, busy_timeout=15000）+ 本地文件目录         │
│  data/db/app.sqlite3   data/files/（原始文档）                 │
│  data/secrets/（Fernet 密钥，用于加密供应商 API Key）          │
└──────────────────────────────────────────────────────────────┘
```

sidecar 启动细节（`backend/app/desktop_entry.py`、`frontend/electron/main.cjs:177-306`，即 `startBackend()` / `stopBackend()`）：

- **动态端口**：`WRK_PORT=0` 时 `bind((host, 0))` 让系统分配，多实例共存不冲突；指定端口被占用则自动回落。
- **就绪上报**：`announce()` 线程最多轮询 600 × 50 ms，确认端口可连后打印 `WRK_READY {"host","port","version","app","pid"}`；主进程 60 秒未收到即失败并给出中文原因。
- **打包后路径**：`process.resourcesPath/backend/wrk-backend.exe`（PyInstaller `--onefile`）；开发环境回落到 `.venv/Scripts/python.exe -m app.desktop_entry`。
- **数据目录**：打包版由主进程注入 `WRK_DATA_DIR=<userData>/data`（通常是 `%APPDATA%\wiki-rag-desktop\data`），卸载重装不丢库（`electron-builder.yml` 中 `deleteAppDataOnUninstall: false`）。

---

## 3. 快速开始

以下命令在仓库根目录用 Git Bash 执行，路径按本仓库实际风格书写（`.venv/Scripts/python.exe`）。

### 3.1 一次性准备

```bash
cd /f/GItHub_Code/Wiki-Rag-Knowledge

# 1) 后端虚拟环境 + 依赖（仓库已带 .venv，可跳过创建）
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r backend/requirements.txt

# 2) 前端依赖
cd frontend && pnpm install && cd ..
#    没有全局 pnpm 时用：node <pnpm.mjs 路径> install（见 scripts/dev.sh:25 的回退写法）
```

### 3.2 开发模式（两个终端）

```bash
# 终端 A：后端 http://127.0.0.1:8000 ，接口文档 /docs
bash scripts/start_backend.sh

# 终端 B：vite(5173) + Electron 窗口；后端由 Electron 自己拉起
cd frontend && node scripts/dev.mjs
```

只要浏览器调试界面时用 `node scripts/dev.mjs --web-only`（不启 Electron，直接开 `http://localhost:5173`）。

一键脚本 `bash scripts/dev.sh` 会把「建 venv → 装依赖 → 灌演示数据 → 同时起前后端」串起来（设 `SKIP_SEED=1` 可跳过灌数据）。

### 3.3 灌演示数据（可选）

```bash
cd backend
PYTHONIOENCODING=utf-8 PYTHONPATH=. ../.venv/Scripts/python.exe -m scripts.seed_demo
```

生成 3 个知识库（人事制度库 `wiki` / 客户支持库 `basic` / 产品与销售库 `basic`）、4 个账号（1 管理员 + 3 员工，含库级授权）与 3 份演示文档。脚本幂等，`--reset` 先清空业务数据。演示账号密码见 `backend/scripts/seed_demo.py` 的 `DEMO_PASSWORD`。

### 3.4 首次启动引导

首次启动**不会**自动创建管理员（`backend/app/main.py:27-49`）：没有管理员时 `GET /api/setup/status` 返回 `initialized=false`，前端跳到 `/setup` 由用户自己设账号与密码（密码至少 10 位，且需含大写/小写/数字/符号中的至少三类，`backend/app/security.py` 的 `password_strength_error()`）。

无人值守场景（开发、CI、脚本）可显式给初始密码：

```bash
WRK_ADMIN_PASSWORD='<你的初始密码>' bash scripts/start_backend.sh
```

此时启动即建号，并打印横幅提示「首次登录后必须修改密码」。**已有管理员时永不覆盖。**

### 3.6 自动更新

桌面版内置 `electron-updater`，安装版会在启动后与每 4 小时检查一次更新，下载完成提示重启安装（不动知识库数据）。完整说明见 [docs/UPDATE.md](docs/UPDATE.md)。

- 默认更新源：GitHub Releases（`electron/update-config.json`，**仓库必须公开**）
- 自建源：`node scripts/build.mjs` 会把 `latest.yml` + 安装包 + `.blockmap` 收集到 `release/updates/`，传到任意 HTTPS 静态目录，配置 `provider: generic` + `url`
- 本地联调：`WRK_UPDATE_URL=http://127.0.0.1:8899 ./release/win-unpacked/企业知识库.exe`
- 发版：改 `frontend/package.json` 的 `version` → `node scripts/build.mjs --publish`（GitHub）或手动上传 `release/updates/`

### 3.5 打包成 Windows 安装包

```bash
cd frontend
node scripts/build.mjs            # NSIS 安装包 → frontend/release/
node scripts/build.mjs --pack     # 只出 release/win-unpacked/，不生成安装包（快速验证）
```

可复用的分步开关：`--skip-backend`（复用 `build/backend/wrk-backend.exe`）、`--skip-web`（复用 `dist/`）。单独打后端：`node scripts/build-backend.mjs`。

打包前必须清掉本机全局的 `ELECTRON_RUN_AS_NODE`——脚本里的 `cleanEnv()` 已自动 `delete`（`frontend/scripts/build.mjs:53-57`），手工执行 `electron-builder` 时要自己 `env -u ELECTRON_RUN_AS_NODE`。上一次构建残留的 `wrk-backend.exe` 会锁住输出目录，报 `EPERM` 时先 `taskkill /F /IM wrk-backend.exe`。

安装向导默认是**简体中文**。改语言时注意 `electron-builder.yml` 里两个开关必须配合：`installerLanguages: [zh_CN]` 只有在 `multiLanguageInstaller: true` 时才参与编译（设成 `false` 会被 `LangConfigurator` 强制成 `en_US`）；`language: "2052"` 只影响版本资源的 locale id。只给一种语言时 NSIS 不会弹语言选择框。

---

## 4. 目录结构

```
Wiki-Rag-Knowledge/
├── backend/
│   ├── app/
│   │   ├── config.py             # 全部可调参数（环境变量覆盖）
│   │   ├── main.py               # FastAPI 入口、引导管理员、/api/meta
│   │   ├── desktop_entry.py      # sidecar 入口：动态端口 + WRK_READY
│   │   ├── models.py             # 19 张表
│   │   ├── db.py                 # 引擎、WAL、老库补列自愈
│   │   ├── deps.py               # 鉴权、库级授权、审计
│   │   ├── routers/              # auth setup kbs wiki chat settings
│   │   ├── services/
│   │   │   ├── parser.py         # 六类格式 → ParsedBlock/TableData
│   │   │   ├── chunking.py       # 文本块 / 表格行组 / 关键词抽取
│   │   │   ├── ingest.py         # 入库流水线（解析→分块→向量化→下线旧版本）
│   │   │   ├── worker.py         # 进程内任务队列
│   │   │   ├── retrieval.py      # 两通道检索 + 两层融合
│   │   │   ├── answering.py      # 提示词、引用校验、拒答、建议
│   │   │   ├── wiki.py           # Map-Reduce 主题归纳与页面生成
│   │   │   ├── llm.py            # 三协议适配 + CallStats
│   │   │   └── cost.py           # 成本流水与聚合
│   │   └── ...
│   ├── scripts/                  # seed_demo seed_expanded inspect_chunks collect_metrics
│   ├── tests/                    # 17 个文件，166 项
│   └── requirements.txt
├── frontend/
│   ├── electron/main.cjs         # sidecar 生命周期、失败原因翻译、日志
│   ├── scripts/                  # dev.mjs build.mjs build-backend.mjs
│   ├── src/                      # pages / components / api.ts / auth.tsx
│   └── electron-builder.yml      # NSIS 配置
├── demo_data/                    # 3 篇演示文档 + fixtures（表格压力样本）
├── docs/                         # DESIGN.md API.md EXPERIMENT.md HANDOFF.md
├── scripts/                      # dev.sh start_backend.sh start_frontend.sh 等
└── data/                         # 运行时生成（git 忽略）
```

---

## 5. 配置项

全部可调项集中在 `backend/app/config.py`，均可用环境变量覆盖。默认值即下表「默认」列。

### 5.1 路径与进程

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `WRK_DATA_DIR` | `<repo>/data` | 数据根目录（打包版由 Electron 注入到 userData 下） |
| `WRK_DB_PATH` | `<WRK_DATA_DIR>/db/app.sqlite3` | SQLite 文件路径 |
| `WRK_SECRET_DIR` | `<WRK_DATA_DIR>/secrets` | Fernet 密钥目录（加密供应商 API Key） |
| `WRK_DATABASE_URL` | `sqlite:///<DB_PATH>` | 覆盖整条连接串 |
| `WRK_WORKER_ENABLED` | `1` | 是否启动进程内 Worker（测试里置 `0`） |
| `WRK_WORKER_POLL_SECONDS` | `1.0` | 任务轮询间隔 |
| `WRK_WORKER_CONCURRENCY` | `2` | Worker 线程数 |
| `WRK_HOST` / `WRK_PORT` | `127.0.0.1` / `0` | sidecar 监听地址；`0` = 系统分配 |
| `WRK_WATCH_STDIN` | `1` | stdin EOF 时自退（Electron 被强杀也能收尾） |
| `WRK_LOG_LEVEL` | `info` | uvicorn 日志级别 |
| `WRK_PACKAGED` | — | 打包环境由主进程置 `1` |

### 5.2 上传与安全

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `WRK_MAX_UPLOAD_MB` | `50` | 单文件上限，超限拒绝并给出中文原因 |
| `WRK_ADMIN_USERNAME` | `admin` | 自动建号时的用户名 |
| `WRK_ADMIN_PASSWORD` | 空 | **非空才自动建号**；空则等用户在 `/setup` 建 |
| `WRK_TOKEN_TTL_MINUTES` | `720` | JWT 有效期（令牌无状态，权限每次实时查库） |
| `WRK_PBKDF2_ITER` | `240000` | 口令哈希迭代次数（测试里降到 2000 提速） |
| `WRK_CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173` | 开发态跨域白名单 |

### 5.3 检索与分块

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `WRK_CHUNK_SIZE` | `700` | 正文块目标字符数 |
| `WRK_CHUNK_OVERLAP` | `120` | 同标题内块尾重叠；标题/表格边界恒为 0 |
| `WRK_TABLE_CHUNK_BUDGET` | `1600` | 表格块字符预算（硬约束，按行组装填） |
| `WRK_RRF_K` | `60` | RRF 平滑常数 |
| `WRK_PER_KB_CANDIDATES` | `8` | 每库先取多少候选 |
| `WRK_MAX_EVIDENCE` | `10` | 最终证据条数上限 |
| `WRK_CONTEXT_CHAR_BUDGET` | `9000` | 拼进提示词的上下文字符上限 |

### 5.4 Wiki 生成

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `WRK_WIKI_SHARD_CHAR_BUDGET` | `=CONTEXT_CHAR_BUDGET`（9000） | 单片 Map 调用的资料总长 |
| `WRK_WIKI_TOPIC_EXCERPT_CHARS` | `900` | 单片内单块摘录长度 |
| `WRK_WIKI_TOPIC_EXCERPT_MIN` | `120` | 片数超限时摘录可压缩到的下限 |
| `WRK_WIKI_MAX_MAP_CALLS` | `40` | Map 调用上限；`0` = 不限制。超限先压摘录，压到下限仍超就**如实多调用**（不丢库尾内容） |
| `WRK_WIKI_MAX_PAGES` | `60` | 合并后页面数上限；超限按证据数保留并在 `notes` 说明 |

### 5.5 网络与建议文案

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `WRK_HTTP_TRUST_ENV` | `0` | **默认不继承系统代理**，避免把 API Key 送进公司代理 |
| `WRK_HTTP_PROXY` | 空 | 需要代理时显式指定，如 `http://proxy.corp:8080` |
| `WRK_HTTP_TIMEOUT_READ` | `120` | 供应商读取超时（秒） |
| `WRK_SUGGESTION_LIMIT` | `5` | 拒答时「你可以试试这样问」条数 |
| `WRK_SUGGESTION_RESOURCE_LIMIT` | `6` | 拒答时「本次检索到这些资料」条数 |

前端构建期变量：`WRK_DEV=1`、`WRK_DEV_URL`（dev.mjs 注入）、`WRK_NODE`、`WRK_PYTHON`（覆盖解释器路径）。

---

## 6. 模型配置

界面在 `/admin/models`，接口在 `/api/settings/*`。供应商（Provider）→ 模型（ModelConfig，`purpose` = `llm` / `embedding` / `vision`，各自可设默认）。

### 6.1 三种协议

| `protocol` | 生成调用 | Embedding | 鉴权头 |
| --- | --- | --- | --- |
| `openai`（默认） | `POST {base}/chat/completions` | `POST {base}/embeddings` | `Authorization: Bearer` |
| `openai-responses` | `POST {base}/responses` | **无** | `Authorization: Bearer` |
| `anthropic` | `POST {base}/messages` | **无** | `x-api-key` + `anthropic-version` |

`openai-responses` 与 `anthropic` 没有 Embedding 端点，`llm.embed()` 会**直接抛错**而不是悄悄退回哈希向量（`backend/app/services/llm.py:636-639`、`backend/app/services/llm.py:466-471`）——静默降级会让用户以为向量检索正常，实际排序质量已经塌了。向量用途需另配一个 OpenAI 兼容供应商。

### 6.2 探测与导入

`POST /api/settings/providers/{id}/discover` 探测供应商侧模型列表，**永远返回 200 + 结果对象**（`items / source / error / total`）：很多中转没有 `/models`，探测失败是正常业务状态。Anthropic 探测失败时回退内置清单（`source="builtin"`）。随后 `POST .../models/import` 批量导入，按 `(provider, purpose, model)` 幂等，已存在的跳过。

### 6.3 换 Embedding = 强制全量重建

每个库用 `embed_signature`（如 `openai:text-embedding-3-small:1536`）标记向量来源，检索只取签名一致的块——新旧模型的维度与语义空间都不同，混用会让余弦相似度失去意义。

`POST /api/settings/embedding/switch`：已有索引时必须带 `confirm=true`，否则 409；确认后为每个有索引的库建 `embedding_rebuild` 任务全量重算。`GET /api/settings/embedding/status` 给出 `active`、各库 `mismatch`、`require_confirm` 与正在跑的重建任务。

### 6.4 重试 = 同一条任务记录回退，不是再插一条

任务表是**执行记录**，不是重试日志。旧实现每点一次「重试」就 `INSERT` 一条新任务：点三次任务列表多三条，同一份文档还会被并发解析三遍。现在：

- `POST /documents/{id}/reindex`（文档列表的重试入口）：找到该文档最新版本对应的那条入库任务**原地打回队列**，次数记在 `payload.retry.count`，`attempts` 归零重新累计；没有任务记录的历史文档才补建一条。
- `POST /tasks/{id}/retry`（任务中心的兜底入口）：同样的原地回退；任务已在排队/执行中直接 409，不会堆出重复任务。
- 状态双向同步走 `services/taskflow.py`：重试 → 版本/文档/知识库一起回到「索引中」；执行失败或中断恢复放弃 → 版本/文档标失败、知识库按「还有没有可用索引」决定 `partial` / `failed`。任务列表说失败、文档却停在「索引中」这种自相矛盾不会再出现。
- 文档列表的状态列以**最新上传的版本**为准（`latest_version`），而不是「已生效的版本」（`current_version`）——新版本失败时前者才有意义。

---

## 7. 接口速览

完整契约见 [docs/API.md](docs/API.md)。前缀 `/api`，除登录与少量初始化接口外都要 `Authorization: Bearer <token>`。

| 分组 | 代表接口 |
| --- | --- |
| 初始化 | `GET /setup/status`、`POST /setup/admin`（仅无管理员时可调，之后永久 409）、`GET /setup/org` |
| 认证 | `POST /auth/login`、`GET /auth/me`、`POST /auth/change-password` |
| 知识库 | `GET/POST /kbs`、`PATCH /kbs/{id}`、`POST /kbs/{id}/rebuild`、`GET /kbs/{id}/cost` |
| 文档与任务 | `POST /kbs/{id}/documents`、`GET /documents/{id}/chunks`、`POST /documents/{id}/reindex`（文档级重试）、`GET /tasks`、`POST /tasks/{id}/retry` |
| Wiki | `GET /kbs/{id}/wiki/tree`、`PUT /kbs/{id}/wiki/pages/{slug}`（人工校对）、`POST .../unlock` |
| 问答 | `POST /conversations/{id}/ask`、`POST /search/preview`（只检索不生成，实验用） |
| 模型设置 | `GET/POST /settings/providers`、`POST /settings/providers/{id}/discover`、`POST /settings/embedding/switch` |

`/api/meta` 与 `/api/health` 无需登录，分别用于前端判断 `needs_setup` / `offline_models` 与存活探测。

---

## 8. 测试与验收

```bash
# 全量后端测试（自带临时数据目录，不污染 data/）
.venv/Scripts/python.exe -m pytest backend/tests -q

# 只跑某个主题
.venv/Scripts/python.exe -m pytest backend/tests/test_wiki_scale.py -q
```

测试覆盖：入库流水线、表格分块、检索质量、权限、拒答判定、成本口径、模型协议、首次引导、失败语义、老库补列、Wiki 规模、**写锁韧性（`test_lock_resilience.py` / `test_responsive_during_embed.py`）**、**进程守护（`test_procguard.py`）**、**中断恢复与重试语义（`test_resume_and_locks.py` / `test_retry_semantics.py`）**。表格测试需要 `demo_data/fixtures/` 的样本，缺失时会 `pytest.skip` 并提示先跑 `python scripts/make_fixtures.py`。

前端类型检查与构建：

```bash
cd frontend && pnpm typecheck && pnpm build:web
```

实验数据采集（后端需已启动）：

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe backend/scripts/collect_metrics.py \
  --base-url http://127.0.0.1:8000 --username admin --password '你的密码' \
  --input questions.json --out results.csv --mode search --repeat 3
```

---

## 9. 常见问题

**Q：`ELECTRON_RUN_AS_NODE` 导致窗口起不来 / `ipcMain` 是 undefined。**
本机若全局设了该变量，Electron 会退化成纯 Node，`ipcMain.handle` 直接报错。`frontend/scripts/dev.mjs` 与 `frontend/scripts/build.mjs` 都会在 spawn 前删掉它；手工启动时要 `env -u ELECTRON_RUN_AS_NODE node_modules/.bin/electron .`。

**Q：端口被占用。**
打包版固定传 `WRK_PORT=0`，由系统分配，通常不会冲突；`_pick_port()` 在指定端口被占时也会自动回落。开发态 `start_backend.sh` 用 8000、vite 用 5173（`--strictPort`，被占则报错退出），换端口：`WRK_PORT=8010 bash scripts/start_backend.sh`、`node frontend/scripts/dev.mjs --port 5174`。

**Q：忘记管理员密码。**
没有后门重置接口。关掉程序，删除数据目录（开发态 `<repo>/data`，打包版 `%APPDATA%\wiki-rag-desktop\data`），重启后回到 `/setup` 重新建号。**这会连库一起删**，只想重置密码就先用管理员账号登录改，或删库前备份 `data/files/`。

**Q：没配模型能干什么。**
全链路可跑：LLM 侧退回抽取式整理与抽取式回答，Embedding 侧退回 256 维确定性哈希向量，向量通道权重降到 0.45。适合验收功能与跑通流程，**但检索排序与回答质量不能作为实验数据**。

**Q：上传报「超过 50MB 上限」。**
`WRK_MAX_UPLOAD_MB` 控制，改环境变量后重启后端。格式只接受 `.txt .md .markdown .log .pdf .docx .xlsx .pptx`，其余直接拒绝（`backend/app/config.py:27`）。

**Q：库里明明有文档，回答却说「现有资料不足以回答这个问题」。**
先看响应里的 `route.kb_routes[]`：`used="none"` 说明该库当前不可检索（Wiki 未就绪且无分块），`fallback=true` 说明发生了检索方式回退。拒答文案本身也会列出这两类库与原因，以及本次真实命中的资料和建议问法。

**Q：`data/` 和构建产物会被提交进 git 吗。**
不会。`.gitignore` 已忽略 `data/`、`*.sqlite3`、`secrets/`、`backend/uploads/`，以及构建产物目录
`frontend/release/`、`frontend/build/backend/`、`frontend/build/pyinstaller/`（`.gitignore:36-39`；
`git ls-files frontend/release` 为 0）。硬编码本机路径的临时探针 `frontend/.tmp-probe-ud.mjs` 已删除，
git 里是删除状态。

---

## 10. 文档索引

| 文件 | 内容 |
| --- | --- |
| [docs/DESIGN.md](docs/DESIGN.md) | 系统设计说明：分层、数据模型、分块、检索融合、证据链、权限、一致性 |
| [docs/API.md](docs/API.md) | 接口契约 v1（请求/响应字段、`usage` 口径、成本聚合结构） |
| [docs/EXPERIMENT.md](docs/EXPERIMENT.md) | 实验方案：数据集、四个对照实验、指标定义、公平性陷阱 |
| [docs/THESIS.md](docs/THESIS.md) | 论文可复用章节素材：设计、关键实现、实验方法、创新点与局限 |
| [docs/HANDOFF.md](docs/HANDOFF.md) | 交接说明 |

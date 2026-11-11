# 企业知识库文档整理与问答系统

[![CI](https://github.com/guanmuya/wiki-rag-knowledge/actions/workflows/ci.yml/badge.svg)](https://github.com/guanmuya/wiki-rag-knowledge/actions/workflows/ci.yml)

面向单个企业的多知识库文档整理与增强问答桌面程序。文档入库后同时产出两种可检索形态：**原文分块索引**（关键词 + 向量）与 **Wiki 增强索引**（模型归纳成页面与章节），问答时按库自动路由、融合召回、强制标注引用编号，并在资料不足时给出结构化的「缺什么 + 可以怎么问」。

后端是 FastAPI + SQLAlchemy + SQLite 的单机进程，前端是 React + Vite，Electron 负责把后端作为 **sidecar 子进程**拉起并动态分配端口。没有外部队列、没有独立数据库服务、不需要运维。

- 应用名 `企业知识库文档整理与问答系统`，版本 `1.0.18`（`backend/app/main.py` 的 `VERSION` 常量；`frontend/package.json` 的 `version` 同为 `1.0.18`）
- 安装包产品名 `企业知识库`，appId `com.wikiraq.knowledge`（`frontend/electron-builder.yml`）
- 后端测试基线见 [docs/FACTS.md](docs/FACTS.md) §2（全仓唯一记录，附一次真实运行输出；本页不复述数字）

> **事实口径**：版本、表数、Worker 并发、SQLite 锁超时、测试基线以代码为唯一事实源，
> 权威值统一在 [docs/FACTS.md](docs/FACTS.md)；本页与其他文档只放链接，不再各自写死数字。

---

## 1. 核心能力

| 能力 | 实现要点 | 代码依据（文件 + 符号名） |
| --- | --- | --- |
| 六类文档解析 | `pdf / docx / md / txt / xlsx / pptx`；图片只记元信息不解析 | `backend/app/services/parser.py` 的 `parse_document()`；`backend/app/config.py` 的 `ALLOWED_SUFFIXES` |
| 文本分块 | 目标 700 字符；**标题变化强制切分**，重叠只在同标题内生效 | `backend/app/services/chunking.py` 的 `split_blocks()` |
| 表格分块 | 按字符预算切行组 + 每块重复表头 + `locator` 记录行区间 | `backend/app/services/chunking.py` 的 `split_table()` |
| 双形态索引 | `basic` = 分块；`wiki` = 主题归纳 → 页面 → 章节 | `backend/app/models.py` 的 `KnowledgeBase.mode` / `Chunk`；`backend/app/services/wiki.py` 的 `build_wiki()` |
| Wiki Map-Reduce | 全库按字符预算分片归纳，片内用全库全局编号，合并去重 | `backend/app/services/wiki.py` 的 `_plan_shards()` 与 `build_wiki()` |
| 两层融合检索 | 库内**加权 RRF**；跨库**覆盖率 × 名次衰减 × 均衡因子** | `backend/app/services/retrieval.py` 的 `rrf_fuse()` / `channel_weights()`（库内）与 `retrieve()`（跨库） |
| 检索链路 | 意图分析 → 查询路由（original/rewrite/multi_query）→ 多查询检索 → 覆盖度评估 → 缺失查询重写（最多 2 次）→ 回答生成 | `backend/app/services/qa_pipeline.py` |
| 流式思考过程 | SSE 推送意图、查询、检索命中、评估、重试与答案增量；链路快照随消息落库 | `backend/app/routers/chat.py`、`frontend/src/pages/ChatPage.tsx` |
| 逐库自动路由 | `wiki` 未就绪回退分块并标 `fallback`，不静默降级 | `backend/app/services/retrieval.py` 的 `plan_route()` / `retrieve()` |
| 证据链 | 回答 `[编号]` → `Message.evidence` → `Chunk.locator` / `WikiEvidence` | `backend/app/services/answering.py` 的 `_citation_check()` 与 `answer()` |
| 结构化拒答 | 固定前置句 + 本次命中资料 + 可点击建议问题 | `backend/app/services/answering.py` 的 `_refusal_verdict()` / `_suggestion_block()` |
| 成本核算 | `CallStats` 分 chat/embed/vision 三通道，流水落 `cost_records` | `backend/app/services/llm.py` 的 `CallStats`；`backend/app/services/cost.py` 的 `record()` |
| 三协议模型适配 | `openai` / `openai-responses` / `anthropic` | `backend/app/services/llm.py` 的 `SUPPORTED_PROTOCOLS`；`backend/app/models.py` 的 `ProviderProtocol` |
| 首次启动引导 | 无管理员时前端走 `/setup` 建号，不再随机生成密码 | `backend/app/routers/setup.py`、`backend/app/main.py` 的 `bootstrap_admin()` |
| 个人 Agent | 核心插件注册表（知识库 / 本机 CLI），会话内可换核心、复用已配置模型 | `backend/app/services/agent/`、`frontend/src/pages/AgentPage.tsx` |
| 离线可跑 | 无模型时 LLM 抽取式、Embedding 256 维哈希向量 | `backend/app/services/llm.py` 的 `FALLBACK_EMBED_DIM` / `hash_embed()`；`backend/app/services/answering.py` 的 `_extractive_answer()` |

> 行号会随重构漂移，故本表用「文件 + 符号名」定位；定位约定与常用符号对照见
> [docs/FACTS.md](docs/FACTS.md) §3。

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
│  routers: auth setup kbs wiki chat settings agent              │
│  services: parser chunking llm retrieval answering wiki cost   │
│            ingest worker agent（核心注册表 + 运行器）          │
│  进程内 Worker 线程 ×1（WORKER_CONCURRENCY=1）轮询 tasks 表     │
└───────────────┬──────────────────────────────────────────────┘
                │
┌───────────────▼──────────────────────────────────────────────┐
│ SQLite 单文件（WAL，写锁三档 3s/30s/1s）+ 本地文件目录         │
│  data/db/app.sqlite3   data/files/（原始文档）                 │
│  data/secrets/（Fernet 密钥，用于加密供应商 API Key）          │
└──────────────────────────────────────────────────────────────┘
```

sidecar 启动细节（`backend/app/desktop_entry.py`，以及 `frontend/electron/main.cjs` 的 `startBackend()` / `stopBackend()`）：

- **动态端口**：`WRK_PORT=0` 时 `bind((host, 0))` 让系统分配，多实例共存不冲突；指定端口被占用则自动回落。
- **就绪上报**：`announce()` 线程最多轮询 600 × 50 ms，确认端口可连后打印 `WRK_READY {"host","port","version","app","pid"}`；主进程 60 秒未收到即失败并给出中文原因。
- **打包后路径**：`process.resourcesPath/backend/wrk-backend.exe`（PyInstaller `--onefile`）；开发环境回落到 `.venv/Scripts/python.exe -m app.desktop_entry`。
- **数据目录**：打包版由主进程注入 `WRK_DATA_DIR=<userData>/data`（通常是 `%APPDATA%\wiki-rag-desktop\data`），卸载重装不丢库（`electron-builder.yml` 中 `deleteAppDataOnUninstall: false`）。

---

## 3. 快速开始

以下命令都在**仓库根目录**执行（Windows + Git Bash；Linux / macOS 把 `.venv/Scripts/python.exe` 换成 `.venv/bin/python`）。后端固定使用 Python 3.13，依赖版本由 `backend/requirements*.txt` 与 `backend/constraints.txt` 锁定，CI 用的是同一份锁文件。

### 3.1 一次性准备

仓库**不自带 `.venv`**：首次克隆后必须自行创建，不要跳过这一步，否则打包脚本找不到解释器。

```bash
# 1) 后端虚拟环境 + 锁定的依赖（含测试分组）
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r backend/requirements-dev.txt -c backend/constraints.txt

# 2) 前端依赖（严格按 pnpm-lock.yaml 安装）
cd frontend && pnpm install --frozen-lockfile && cd ..
#    没有全局 pnpm 时用：corepack pnpm install 或 npx --yes pnpm@9 install（见 scripts/dev.sh）
```

> **依赖分组与锁文件**
> - `backend/requirements.txt`：13 个运行时依赖，全部 `==` 精确锁定（本文件即运行时锁）。
> - `backend/constraints.txt`：传递依赖的精确版本；任何 `pip install` 都要带 `-c backend/constraints.txt`。
> - `backend/requirements-dev.txt`：运行时 + `pytest`（本地开发与 CI 用这一份）。
> - `backend/requirements-build.txt`：运行时 + 固定版本的 PyInstaller（仅打 exe 时用，由 `frontend/scripts/build-backend.mjs` 自动安装）。
> - `frontend/pnpm-lock.yaml`：前端锁文件，配合 `--frozen-lockfile` 使用。
>
> 升级依赖时同步更新上述文件，并在 CI 上重跑一次全量测试；测试基线的更新口径见 [docs/FACTS.md](docs/FACTS.md) §2.4。

### 3.2 开发模式（两个终端）

```bash
# 终端 A：后端 http://127.0.0.1:8000，接口文档 /docs
bash scripts/start_backend.sh

# 终端 B：vite(5173) + Electron 窗口；后端由 Electron 自己拉起
cd frontend && node scripts/dev.mjs
```

只想用浏览器调试界面时，用 `node scripts/dev.mjs --web-only`（不启 Electron，直接开 `http://localhost:5173`）。

一键脚本 `bash scripts/dev.sh` 会把「建 venv → 装依赖 → 灌演示数据 → 同时起前后端」串起来（设 `SKIP_SEED=1` 可跳过灌数据）。

### 3.3 灌演示数据（可选）

```bash
cd backend
PYTHONIOENCODING=utf-8 PYTHONPATH=. ../.venv/Scripts/python.exe -m scripts.seed_demo
```

生成 3 个知识库（人事制度库 `wiki` / 客户支持库 `basic` / 产品与销售库 `basic`）、4 个账号（1 管理员 + 3 员工，含库级授权）与 3 份演示文档。脚本幂等，`--reset` 先清空业务数据。演示账号密码见 `backend/scripts/seed_demo.py` 的 `DEMO_PASSWORD`。

### 3.4 首次启动引导

首次启动**不会**自动创建管理员（`backend/app/main.py` 的 `bootstrap_admin()`，`backend/app/routers/setup.py`）：没有管理员时 `GET /api/setup/status` 返回 `initialized=false`，前端跳到 `/setup` 由用户自己设账号与密码（密码至少 10 位，且需含大写/小写/数字/符号中的至少三类，`backend/app/security.py` 的 `password_strength_error()`）。

无人值守场景（开发、CI、脚本）可显式给初始密码：

```bash
WRK_ADMIN_PASSWORD='<你的初始密码>' bash scripts/start_backend.sh
```

此时启动即建号，并打印横幅提示「首次登录后必须修改密码」。**已有管理员时永不覆盖。**

### 3.5 打包成 Windows 安装包

```bash
cd frontend
node scripts/build.mjs            # NSIS 安装包 → frontend/release/
node scripts/build.mjs --pack     # 只出 release/win-unpacked/，不生成安装包（快速验证）
```

可复用的分步开关：`--skip-backend`（复用 `build/backend/wrk-backend.exe`）、`--skip-web`（复用 `dist/`）。单独打后端：`node scripts/build-backend.mjs`。

打包前必须清掉本机全局的 `ELECTRON_RUN_AS_NODE`——脚本里 `frontend/scripts/build.mjs` 的 `cleanEnv()` 已自动 `delete`，手工执行 `electron-builder` 时要自己 `env -u ELECTRON_RUN_AS_NODE`。上一次构建残留的 `wrk-backend.exe` 会锁住输出目录，报 `EPERM` 时先 `taskkill /F /IM wrk-backend.exe`。

安装向导默认是**简体中文**。改语言时注意 `electron-builder.yml` 里两个开关必须配合：`installerLanguages: [zh_CN]` 只有在 `multiLanguageInstaller: true` 时才参与编译（设成 `false` 会被 `LangConfigurator` 强制成 `en_US`）；`language: "2052"` 只影响版本资源的 locale id。只给一种语言时 NSIS 不会弹语言选择框。

### 3.6 自动更新

桌面版内置 `electron-updater`，安装版会在启动后以及每 4 小时检查一次更新，下载完成提示重启安装（不动知识库数据）。完整说明见 [docs/UPDATE.md](docs/UPDATE.md)。

- 默认更新源：GitHub Releases（`electron/update-config.json`，**仓库必须公开**）
- 自建源：`node scripts/build.mjs` 会把 `latest.yml` + 安装包 + `.blockmap` 收集到 `release/updates/`，传到任意 HTTPS 静态目录，配置 `provider: generic` + `url`
- 本地联调：`WRK_UPDATE_URL=http://127.0.0.1:8899 ./release/win-unpacked/企业知识库.exe`
- 发版：改 `frontend/package.json` 的 `version` → `node scripts/build.mjs --publish`（GitHub）或手动上传 `release/updates/`

---

## 4. 目录结构

```
Wiki-Rag-Knowledge/
├── backend/
│   ├── app/
│   │   ├── config.py             # 全部可调参数（环境变量覆盖）
│   │   ├── main.py               # FastAPI 入口、引导管理员、/api/meta
│   │   ├── desktop_entry.py      # sidecar 入口：动态端口 + WRK_READY
│   │   ├── models.py             # 23 张表（22 个 __tablename__ + migrations 的 schema_migrations）
│   │   ├── migrations.py         # 老库自愈迁移（schema_migrations 账本，P1-8）
│   │   ├── db.py                 # 引擎、WAL、老库补列自愈
│   │   ├── deps.py               # 鉴权、库级授权、审计
│   │   ├── routers/              # auth setup kbs wiki chat settings agent
│   │   ├── services/
│   │   │   ├── parser.py         # 六类格式 → ParsedBlock/TableData
│   │   │   ├── chunking.py       # 文本块 / 表格行组 / 关键词抽取
│   │   │   ├── ingest.py         # 入库流水线（解析→分块→向量化→下线旧版本）
│   │   │   ├── worker.py         # 进程内任务队列
│   │   │   ├── retrieval.py      # 两通道检索 + 两层融合
│   │   │   ├── qa_pipeline.py    # 意图 → 路由 → 检索 → 评估 → 重试 → 回答（SSE 事件源）
│   │   │   ├── answering.py      # 提示词、引用校验、拒答、建议
│   │   │   ├── wiki.py           # Map-Reduce 主题归纳与页面生成
│   │   │   ├── llm.py            # 三协议适配 + CallStats
│   │   │   ├── agent/            # 个人 Agent：核心插件注册表 + 运行器
│   │   │   └── cost.py           # 成本流水与聚合
│   │   └── ...
│   ├── scripts/                  # seed_demo seed_expanded inspect_chunks collect_metrics run_experiments
│   ├── tests/                    # 后端测试（文件数、用例数与基线见 docs/FACTS.md §2）
│   └── requirements.txt
├── frontend/
│   ├── electron/main.cjs         # sidecar 生命周期、失败原因翻译、日志
│   ├── scripts/                  # dev.mjs build.mjs build-backend.mjs + verify-* 人工验收辅助（见 scripts/README.md）
│   ├── src/                      # pages / components / api.ts / auth.tsx
│   ├── tests/                    # 自动化测试（pnpm test，纳入 CI；见 tests/README.md）
│   └── electron-builder.yml      # NSIS 配置
├── demo_data/                    # 3 篇演示文档 + fixtures（表格压力样本）
├── docs/                         # DESIGN.md API.md EXPERIMENT.md HANDOFF.md
│   ├── screenshots/current/      # 「当前版本」界面图，供论文/设计说明引用（见 screenshots/README.md）
│   └── archive/                  # 历史档案：旧看板、历史交接快照、历史 before/after 截图（见 archive/README.md）
├── scripts/                      # 长期维护的入口脚本：dev.sh start_backend.sh start_frontend.sh make_fixtures.py …
│   └── dev-only/                 # 一次性排障与人工验收脚本（不进 CI、不属于交付物，见 dev-only/README.md）
├── LICENSE  CHANGELOG.md  CONTRIBUTING.md  .editorconfig
└── data/                         # 运行时生成（git 忽略）
```

---

## 5. 配置项

主要可调项集中在 `backend/app/config.py`（`WRK_*` 环境变量，**当前条数以
[`docs/FACTS.md`](docs/FACTS.md) §1 为准**——本文件不复述具体数字，避免再次漂移），均可用环境变量覆盖，
默认值即下表「默认」列。另有少数几项由 `backend/app/desktop_entry.py`（`WRK_HOST` / `WRK_PORT` /
`WRK_WATCH_STDIN` / `WRK_LOG_LEVEL`）、`backend/app/security.py`（`WRK_PBKDF2_ITER`）、
`backend/app/services/answering.py`（`WRK_SUGGESTION_LIMIT` / `WRK_SUGGESTION_RESOURCE_LIMIT`）
读取，`WRK_PACKAGED` 由 Electron 主进程注入。逐项对照见 [docs/FACTS.md](docs/FACTS.md) §1。

### 5.1 路径与进程

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `WRK_DATA_DIR` | `<repo>/data` | 数据根目录（打包版由 Electron 注入到 userData 下） |
| `WRK_DB_PATH` | `<WRK_DATA_DIR>/db/app.sqlite3` | SQLite 文件路径 |
| `WRK_SECRET_DIR` | `<WRK_DATA_DIR>/secrets` | Fernet 密钥目录（加密供应商 API Key） |
| `WRK_DATABASE_URL` | `sqlite:///<DB_PATH>` | 覆盖整条连接串 |
| `WRK_WORKER_ENABLED` | `1` | 是否启动进程内 Worker（测试里置 `0`） |
| `WRK_WORKER_POLL_SECONDS` | `1.0` | 任务轮询间隔 |
| `WRK_WORKER_CONCURRENCY` | `1` | Worker 线程数（SQLite 同时只允许一个写者，多开只会互相抢写锁） |
| `WRK_BUSY_TIMEOUT_INTERACTIVE` | `3000` | 交互请求的 SQLite 写锁等待（ms），快速失败不卡界面 |
| `WRK_BUSY_TIMEOUT_WORKER` | `30000` | 后台 Worker 写锁等待（ms） |
| `WRK_BUSY_TIMEOUT_PROGRESS` | `1000` | 任务进度写锁等待（ms），best-effort |
| `WRK_LOCK_RETRY_ATTEMPTS` | `5` | 拿不到锁时的重试次数（兼容旧别名 `WRK_LOCK_RETRY`） |
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
| `WRK_TOKEN_TTL_MINUTES` | `720` | 令牌有效期（分钟）。令牌带用户版本号 `ver`，改密 / 重置密码 / 登出递增版本使旧令牌立即失效；权限每次实时查库 |
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

### 5.6 本地个人 Agent（本波补齐，原 §5 漏列）

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `WRK_AGENT_ENABLED` | `1` | 是否启用个人 Agent |
| `WRK_AGENT_PLUGIN_DIR` | `<WRK_DATA_DIR>/agent_plugins` | 第三方 CLI Agent 的 JSON 清单目录 |
| `WRK_AGENT_WORKSPACE` | `<WRK_DATA_DIR>/agent_workspace` | CLI 核心的默认工作目录（不要指到仓库根） |
| `WRK_AGENT_RUN_TIMEOUT` | `900` | 单轮 Agent 执行的硬超时（秒） |
| `WRK_AGENT_HISTORY_MESSAGES` | `12` | 一轮里最多回放多少条历史消息给 CLI 核心 |
| `WRK_AGENT_HISTORY_CHARS` | `1200` | 回放时单条消息的最大字符数 |

> §5.1 与本节合计补齐了体检清单 D6 指出的 11 个漏列配置项；
> 锁超时三档取代旧文档写的一刀切 15000 ms 超时，口径见
> [docs/FACTS.md](docs/FACTS.md) §1.1。

---

## 6. 模型配置

界面在 `/admin/models`，接口在 `/api/settings/*`。供应商（Provider）→ 模型（ModelConfig，`purpose` = `llm` / `embedding` / `vision`，各自可设默认）。添加供应商时可选常用直连接口预设或自定义地址；请求直接发往配置的供应商，模型列表和可用能力以供应商实际支持为准。

管理员可在「用量概览」（`/admin/usage`）查看近 7 天、近 30 天或全部已记录用量。数据来自知识库处理流水及成功保存的问答；检索调试、连接测试和未保存或失败的问答不包含在内。供应商可能不返回 Token 数，失败或中断的调用也可能消耗用量，所示数字不能当作完整账单、余额或按供应商划分的用量。

### 6.1 三种协议

| `protocol` | 生成调用 | Embedding | 鉴权头 |
| --- | --- | --- | --- |
| `openai`（默认） | `POST {base}/chat/completions` | `POST {base}/embeddings` | `Authorization: Bearer` |
| `openai-responses` | `POST {base}/responses` | **无** | `Authorization: Bearer` |
| `anthropic` | `POST {base}/messages` | **无** | `x-api-key` + `anthropic-version` |

`openai-responses` 与 `anthropic` 没有 Embedding 端点，`llm.embed()` 会**直接抛错**而不是悄悄退回哈希向量——静默降级会让用户以为向量检索正常，实际排序质量已经塌了。向量用途需另配一个支持 Embedding 的 OpenAI 兼容供应商。官方 OpenAI 的 o1/o3/o4 系列使用兼容推理模型的输出参数；阿里云中国区的 `text-embedding-v3/v4` 会按每次最多 10 条文本分批请求。

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

## 7. 个人 Agent（可更换核心）

管理员侧边栏的「个人 Agent」是一个本地 Agent 工作台：会话顶部随时换核心，每个核心都能复用「模型设置」里已配置的模型。

- **知识库助手**（内置）：走现有 RAG 链路，检索知识库、带引用回答；没有模型时仍可抽取式回答。
- **本机 CLI 核心**：内置 Claude Code、Codex CLI、Qwen Code、Aider、Goose 的适配器；本机装了对应命令就会显示为可用。应用按供应商协议把 `base_url` / `api_key` / `model` 注入子进程环境变量，**不改写这些 CLI 的配置文件**。
- **插件核心**：把 JSON 清单放进数据目录的 `agent_plugins/`，点「重新扫描」即可接入任意 CLI Agent；应用不加载第三方代码。
- **随时换核心**：会话按轮无状态，切核心后下一轮由新核心执行，历史保留并留一条切换提示。

入口只对管理员开放（CLI 以应用进程身份执行本机命令）。完整的清单字段、占位符、协议与模型注入规则、安全边界见 **[docs/AGENT_PLUGINS.md](docs/AGENT_PLUGINS.md)**，可直接运行的示例见 [docs/examples/agent-plugin-echo.json](docs/examples/agent-plugin-echo.json)。

---

## 8. 接口速览

完整契约见 [docs/API.md](docs/API.md)。前缀 `/api`，除登录与少量初始化接口外都要 `Authorization: Bearer <token>`。

| 分组 | 代表接口 |
| --- | --- |
| 初始化 | `GET /setup/status`、`POST /setup/admin`（仅无管理员时可调，之后永久 409）、`GET /setup/org` |
| 认证 | `POST /auth/login`、`GET /auth/me`、`POST /auth/change-password`、`POST /auth/logout` |
| 知识库 | `GET/POST /kbs`、`PATCH /kbs/{id}`、`POST /kbs/{id}/rebuild`、`GET /kbs/{id}/cost` |
| 文档与任务 | `POST /kbs/{id}/documents`、`GET /documents/{id}/chunks`、`POST /documents/{id}/reindex`（文档级重试）、`GET /tasks`、`POST /tasks/{id}/retry` |
| Wiki | `GET /kbs/{id}/wiki/tree`、`PUT /kbs/{id}/wiki/pages/{slug}`（人工校对）、`POST .../unlock` |
| 问答 | `POST /conversations/{id}/ask`、`POST /conversations/{id}/ask/stream`（SSE 思考过程）、`POST /search/preview`（只检索不生成，实验用） |
| 模型设置 | `GET/POST /settings/providers`、`POST /settings/providers/{id}/discover`、`POST /settings/embedding/switch` |
| 个人 Agent（admin） | `GET /agent/cores`、`POST /agent/sessions`、`POST /agent/sessions/{id}/messages`、`GET /agent/runs/{id}/events`、`POST /agent/plugins/rescan` |

`/api/meta` 与 `/api/health` 无需登录，分别用于前端判断 `needs_setup` / `offline_models` 与存活探测。

令牌可撤销：改密、管理员重置密码、登出都会让该用户此前签发的令牌立即失效（旧 token 调需鉴权接口返回 401），需重新登录；完整契约见 [docs/API.md](docs/API.md) §1。

---

## 9. 测试与验收

```bash
# 全量后端测试（自带临时数据目录，不污染 data/）
python -m pytest backend/tests -q

# 只跑某个主题
python -m pytest backend/tests/test_wiki_scale.py -q
```

> 用 §3.1 创建的 `.venv` 时，把 `python` 换成 `.venv/Scripts/python.exe`；CI 也用
> 同一份锁文件（`.github/workflows/ci.yml`）。
> **测试基线（计数、环境、1 项 skip 的来源、一次真实运行输出）全仓只记在
> [docs/FACTS.md](docs/FACTS.md) §2**，原始 stdout 在 `docs/evidence/pytest-baseline.txt`；
> 本页不复述数字。CI 每次 push / PR 自动运行同一条命令，并把完整输出存为
> `pytest-baseline` workflow artifact，更新口径见 [docs/FACTS.md](docs/FACTS.md) §2.4。

测试覆盖：入库流水线、表格分块、检索质量、权限、拒答判定、成本口径、模型协议、首次引导、失败语义、老库补列、Wiki 规模、**写锁韧性（`test_lock_resilience.py` / `test_responsive_during_embed.py`）**、**进程守护（`test_procguard.py`）**、**中断恢复与重试语义（`test_resume_and_locks.py` / `test_retry_semantics.py`）**、**Agent 插件与核心切换（`test_agent_plugins.py` / `test_agent_runtime.py`）**。表格测试需要 `demo_data/fixtures/` 的样本，缺失时会 `pytest.skip` 并提示先跑 `python scripts/make_fixtures.py`；当前实跑里唯一的 1 项 skip 来自 `backend/tests/test_tables.py` 的 `pytest.importorskip("reportlab", ...)`（可选依赖未安装，用于生成 PDF 表格夹具），不是漏测。

前端类型检查与构建：

```bash
cd frontend && pnpm typecheck && pnpm build:web
```

前端自动化测试（体检清单 P1-3）：

```bash
cd frontend && pnpm test
```

`pnpm test` 用 Node 内置 `node:test` 加项目已有的 `playwright` 跑 4 条真实浏览器 E2E：
未登录重定向、有效令牌恢复登录态、登录失败提示，以及「登录 → 新建知识库 → 智能问答收到回答」主链路。
测试进程内启动 Vite，所有 `/api/**` 由 Playwright 拦截成确定性内存 mock，
**不启动后端、不需要在线模型或凭据、不需要灌语料**，因此离线可重复、结果稳定。
首次运行前装一次浏览器：`cd frontend && pnpm exec playwright install chromium`。
详见 [frontend/tests/README.md](frontend/tests/README.md)；
`frontend/scripts/verify-*` 是一次性人工验收辅助脚本，不是测试，也不进 CI。

> **CI（`.github/workflows/ci.yml`）**：push 到 `master` 或开 PR 时，在 `windows-latest`
> 上依次运行 `python -m pytest backend/tests -q`、`pnpm typecheck`、`pnpm build:web`、
> `pnpm test` 四项检查（跑 E2E 前单独安装 Playwright Chromium）；
> 后端 Python 固定 3.13、依赖取自 `backend/requirements-dev.txt` +
> `backend/constraints.txt`，前端用 `pnpm install --frozen-lockfile`。任意一步失败即
> CI 失败。要让「CI 全绿」成为合并前置条件，需在 GitHub 仓库
> Settings → Branches 里把 `CI` 加为 `master` 的必需状态检查（这属于仓库设置，无法写进文件）。

实验数据采集（后端需已启动）：

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe backend/scripts/collect_metrics.py \
  --base-url http://127.0.0.1:8000 --username admin --password '你的密码' \
  --input questions.json --out results.csv --mode search --repeat 3
```

---

## 10. 常见问题

**Q：`ELECTRON_RUN_AS_NODE` 导致窗口起不来 / `ipcMain` 是 undefined。**
本机若全局设了该变量，Electron 会退化成纯 Node，`ipcMain.handle` 直接报错。`frontend/scripts/dev.mjs` 与 `frontend/scripts/build.mjs` 都会在 spawn 前删掉它；手工启动时要 `env -u ELECTRON_RUN_AS_NODE node_modules/.bin/electron .`。

**Q：端口被占用。**
打包版固定传 `WRK_PORT=0`，由系统分配，通常不会冲突；`_pick_port()` 在指定端口被占时也会自动回落。开发态 `start_backend.sh` 用 8000、vite 用 5173（`--strictPort`，被占则报错退出），换端口：`WRK_PORT=8010 bash scripts/start_backend.sh`、`node frontend/scripts/dev.mjs --port 5174`。

**Q：忘记管理员密码。**
没有后门重置接口。关掉程序，删除数据目录（开发态 `<repo>/data`，打包版 `%APPDATA%\wiki-rag-desktop\data`），重启后回到 `/setup` 重新建号。**这会连库一起删**，只想重置密码就先用管理员账号登录改，或删库前备份 `data/files/`。

**Q：没配模型能干什么。**
全链路可跑：LLM 侧退回抽取式整理与抽取式回答，Embedding 侧退回 256 维确定性哈希向量，向量通道权重降到 0.45。适合验收功能与跑通流程，**但检索排序与回答质量不能作为实验数据**。

**Q：上传报「超过 50MB 上限」。**
`WRK_MAX_UPLOAD_MB` 控制，改环境变量后重启后端。格式只接受 `.txt .md .markdown .log .pdf .docx .xlsx .pptx`，其余直接拒绝（`backend/app/config.py` 的 `ALLOWED_SUFFIXES`）。

**Q：库里明明有文档，回答却说「现有资料不足以回答这个问题」。**
先看响应里的 `route.kb_routes[]`：`used="none"` 说明该库当前不可检索（Wiki 未就绪且无分块），`fallback=true` 说明发生了检索方式回退。拒答文案本身也会列出这两类库与原因，以及本次真实命中的资料和建议问法。

**Q：`data/` 和构建产物会被提交进 git 吗。**
不会。`.gitignore` 已忽略 `data/`、`*.sqlite3`、`secrets/`、`backend/uploads/`，以及构建产物目录
`frontend/release/`、`frontend/build/backend/`、`frontend/build/pyinstaller/`（`.gitignore:36-39`；
`git ls-files frontend/release` 为 0）。硬编码本机路径的临时探针 `frontend/.tmp-probe-ud.mjs` 已删除，
git 里是删除状态。

---

## 11. 文档索引

| 文件 | 内容 |
| --- | --- |
| [docs/FACTS.md](docs/FACTS.md) | **统一事实口径（单一事实源）**：版本、表数、并发、锁超时、测试基线、代码锚点约定 |
| [docs/HEALTH_CHECK.md](docs/HEALTH_CHECK.md) | 项目体检结论与排好优先级的改进清单（P0 → P2） |
| [docs/DESIGN.md](docs/DESIGN.md) | 系统设计说明：分层、数据模型、分块、检索融合、证据链、权限、一致性 |
| [docs/API.md](docs/API.md) | 接口契约 v1（请求/响应字段、`usage` 口径、成本聚合结构、运维诊断） |
| [docs/EXPERIMENT.md](docs/EXPERIMENT.md) | 实验方案（§1–§7）与配置 A 离线实测结果（§8）；未实测项标「待实验」 |
| [docs/results/](docs/results/README.md) | 第一波实验原始数据、汇总表、数据字典与复现命令 |
| [docs/AGENT_PLUGINS.md](docs/AGENT_PLUGINS.md) | 个人 Agent 核心、CLI 适配与插件清单规范 |
| [docs/THESIS.md](docs/THESIS.md) | 论文可复用章节素材：设计、关键实现、实验方法与结果、创新点与局限 |
| [docs/HANDOFF.md](docs/HANDOFF.md) | 交接说明：v1.0.18 当前状态首屏、已知遗留、如何验证 |
| [docs/archive/HANDOFF_HISTORY.md](docs/archive/HANDOFF_HISTORY.md) | 历史快照归档（v1.0.0 → v1.0.10，历史读数，非当前事实） |
| [docs/archive/README.md](docs/archive/README.md) | 归档索引：旧看板、历史截图、历史交接快照，以及「为什么不删除」 |
| [docs/screenshots/README.md](docs/screenshots/README.md) | 界面图索引：当前版本配图清单、拍摄口径、历史对比图去哪了 |
| [docs/evidence/pytest-baseline.txt](docs/evidence/pytest-baseline.txt) | 测试基线一次真实运行的完整 stdout |
| [docs/evidence/gates-2026-11-11.txt](docs/evidence/gates-2026-11-11.txt) | 五项门禁在本地一次全绿实跑的记录（环境、命令、退出码、真实输出） |
| [docs/RELEASE_NOTES_v1.0.18.md](docs/RELEASE_NOTES_v1.0.18.md) | v1.0.18 交付说明：Release 正文草稿 + 合并后上传/校验命令 |
| [docs/evidence/release-v1.0.18-build.txt](docs/evidence/release-v1.0.18-build.txt) | v1.0.18 构建记录：门禁输出、产物大小、`latest.yml` 原文、安装包 sha512 |
| [CHANGELOG.md](CHANGELOG.md) | 变更记录：v1.0.0 → v1.0.18 的关键变化（数字口径仍以 `docs/FACTS.md` 为准） |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 协作约定：环境准备、五项门禁、提交格式、目录与归档规则、密钥纪律 |
| [LICENSE](LICENSE) | 许可证（MIT） |

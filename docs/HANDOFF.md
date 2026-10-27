# 交接说明

> 最后更新：2026-10-27 17:45 · 用户关机，任务暂停

## 总览（先读这一节，再按需查下面的分工细节）

### 当前状态：**打包产物已验证可运行**，可交付

| 项 | 状态 |
|---|---|
| 后端测试 | **93 passed, 1 skipped** |
| 前端构建 | tsc 0 错误，vite build 成功 |
| 后端 exe | `frontend/build/backend/wrk-backend.exe`（55 MB），独立启动验证通过 |
| **桌面安装目录** | **`frontend/release/win-unpacked/` 已产出并实测启动成功** |
| NSIS 安装包 | 未生成（`build.mjs` 不带 `--pack` 时会生成，需额外下载 nsis 工具链） |

### 已验证的打包产物

```
frontend/release/win-unpacked/
├── 企业知识库.exe                          ← 主程序
├── resources/backend/wrk-backend.exe      ← Python sidecar (54 MB)
└── resources/app.asar                     ← 前端 + Electron 主进程
```

实测启动后：
- `wrk-backend.exe` 进程存在，监听动态端口（本次 53777）
- `GET /api/health` → `{"ok":true,"version":"1.0.0"}`
- `GET /api/meta` → `initialized:true`（说明建表与管理员 bootstrap 都成功）
- 日志写入 `%APPDATA%\wiki-rag-desktop\logs\main.log`

### 下次第一件事

```bash
cd F:\GItHub_Code\Wiki-Rag-Knowledge\backend
PYTHONIOENCODING=utf-8 ../.venv/Scripts/python.exe -m pytest tests/ -q   # 应 93 passed, 1 skipped

# 生成安装包（可选）
cd ../frontend
node scripts/build.mjs                    # 不带 --pack → 生成 NSIS 安装包

# 或直接启动已打包版本
env -u ELECTRON_RUN_AS_NODE "./release/win-unpacked/企业知识库.exe"
```

### 本次会话完成的核心工作

1. **桌面化改造**：Electron 外壳 + Python sidecar，动态端口，`WRK_READY` 就绪上报，**打包产物已验证**
2. **解析与分块重做**（按用户新要求）：
   - 文件类型收窄为 pdf/docx/md/txt/xlsx/pptx
   - 图片不解析（视觉模型/OCR 留后续）
   - **大表格行组切分 + 表头重复**：700 行 → 39 块，max 1560 字符，行区间连续
3. **修 7 个真实缺陷**（全部有测试覆盖）：
   - 老库缺列导致 500 → `ensure_sqlite_columns()` 自愈（真实库验证）
   - 解析失败时库状态与事实不符 → 有旧分块时给 `partial`
   - txt/PDF 章节标题不被识别 → 整篇并成 1 块 → 形态识别修复
   - **Wiki 采样窗口与主题编号错位 → 80 块时 12 主题只出 1 页 → 已修**
   - **`DEFAULT_CHUNK_OVERLAP` 完全不生效 → 已修（同标题内现在真正重叠）**
   - **`coverage` 不对外输出 → 已加进 `Evidence.to_dict()`**
   - 前端契约缺口 → 版本历史补 `content_md`、消息补 `insufficient`/`usage`
4. **检索质量加固**：加权 RRF、覆盖率跨库融合、向量词汇闸门、12 个质量回归测试

### 环境坑（必读）

- `ELECTRON_RUN_AS_NODE=1` 存在于本机，会让 Electron 退化成纯 Node → 必须 `env -u ELECTRON_RUN_AS_NODE`
- 系统 `no_proxy` 含 `[::1]` 会让 httpx 崩 → 代码里已 `trust_env=False`
- Electron 主进程是 `.cjs`（因 package.json 有 `"type": "module"`）
- 前端用 HashRouter，深链是 `#/chat` 形式
- electron-builder 架构参数必须写 `--x64`，写 `--win x64` 会报 `Unknown target: x64`

### 演示账号

`admin` / `Admin@2026`；员工 `lisi` / `Demo@Passw0rd`

### 论文撰写注意（thesis-writer 取证结论）

**以下描述与实现不一致，写论文前必读：**

1. **跨库融合不是 RRF**。`rrf_fuse()` 只用于**库内**（关键词 vs 向量）。
   跨库是 `(0.25 + coverage) × 1/(1+0.45×local_idx) × balance`。
   论文必须分两层写，否则评审对照代码会发现。
2. **`coverage` 已对外输出**（本次已修），`/api/search/preview` 可拿到。
3. **Wiki 与查询向量化的模型调用不计入 `CallStats`**：
   `retrieval.py`、`wiki.py:205`、`routers/wiki.py` 三处 `llm.embed()` 未传 `stats`。
   直接读 `usage.calls` 会系统性低估 Wiki 增强成本。**做成本实验时必须自己统计。**
4. **覆盖率不能当拒答判据**：无关查询仍返回 `coverage>0` 的证据。
   「无答案题」必须人工判定。
5. **覆盖率与 rank 非单调**：「P0 工单」题 rank1 cov=0.38、rank2 cov=0.62。
   指标定义要把「库内名次」和「跨库覆盖率」分开写。

**扩料做实验前必读**：demo 三库只有 25 个检索单元，撑不起 40–60 题。
需要扩到 150–300 块。Wiki 采样窗口现在是 60（`TOPIC_SAMPLE_SIZE`），
超出部分不进 Wiki 但可分块检索，且会在 `notes` 里明确说明。

---

## 以下为各子智能体分工交接

## backend-integrator 交接

> 交接时间：2026-10-27 17:0x。原因：用户关机，任务暂停。
> **重要：本次没有修改任何 `backend/app/**` 源码**，工作区源码与 16:58 的状态一致，
> 基线 `pytest tests/ -q` 仍为 **59 passed**。

### 一、已完成

| 事项 | 结果 |
| --- | --- |
| 基线测试 | 暂停前最后一次：`cd backend && PYTHONIOENCODING=utf-8 ../.venv/Scripts/python.exe -m pytest tests/ -q` → **82 passed, 1 skipped, 1 warning**（59 个原有 + table-chunk-tester 新增的 `test_tables.py`；skip 是 `test_tables.py:358` 的 PDF 表格启发式识别用例，属其用例自身声明；warning 是 starlette TestClient 的 httpx 弃用提示，与本项目无关）。**我接手时的原始基线是 59 passed**，之后增长全部来自 table-chunk-tester 新增的测试文件 |
| Chunk 新增字段是否打挂测试 | **没有**。测试库是每个 session 新建的空库，`create_all` 会带上 `kind`/`meta`，所以基线用例全绿 |
| 改动 1（ALLOWED_SUFFIXES 收窄）代码确认 | `backend/app/config.py:27-30` 确为 `.txt .md .markdown .log .pdf .docx .xlsx .pptx`；上传路由 `backend/app/routers/kbs.py:186` 用 `errors.append(...)` 返回中文提示，不是 500 |
| 改动 2（图片不解析）代码确认 | `backend/app/services/parser.py:398-408` 只记录 `{"name","mime","data": b""}`；`ingest.py:44-48` 只打日志。`parsed.images` 仍非空但内容为空 —— 与描述一致 |
| 改动 3（表格结构化分块）实测 | 见下方「fixture 实测产出」，`TableData` → `split_table()` 行组切分 + 表头重复工作正常，locator 带行区间 |
| 改动 4（Chunk.kind/meta） | 模型定义正确（`models.py:179-180`），`ingest.py:89-90` 正确写入；**但老库缺列，见缺陷 1** |
| 改动 5（desktop_entry） | 手工烟测通过：`WRK_PORT=0` 时输出 `INFO: Uvicorn running on http://127.0.0.1:51621`，进程正常启动、收到关闭后 `Shutting down` 正常退出 |

#### fixture 实测产出（直接跑 `parser.parse_document` + `chunking.split_blocks`，未入库）

| 文件 | blocks | 识别表格 | chunk 数 | 表格 chunk 数 | 备注 |
| --- | --- | --- | --- | --- | --- |
| `员工薪酬明细_120行.docx` | 6（heading 3 / text 2 / table 1） | 1 | 9 | 7 | 表头 9 列，行组切分正常，meta 含 `row_start/row_end/total_rows/cols/caption/part/parts` |
| `员工薪酬明细_200行.xlsx` | 2（table 2） | 2（两个工作表） | 13 | 13 | `Sheet:薪酬明细` + `部门汇总` |
| `员工薪酬明细_700行.docx` | 6 | 1 | 41 | 39 | 700 行切 39 组，无碎片爆炸 |
| `客户投诉处理指引.txt` | 4（text 4） | 0 | 1 | 0 | 449 字节短文，合并成 1 块合理 |
| `差旅与采购管理办法.pdf` | 1（text 1） | 0 | 1 | 0 | 1 页 297 字符，pypdf 抽取正常 |
| `研发流程规范.docx` | 18（heading 5 / text 13） | 0 | 5 | 0 | 多级标题，标题变化强制切块生效 |

**所有 chunk 的 `locator` 均非空**（`empty locator units: 0`），满足「chunks 有 locator」的断言。

### 二、还没做

1. `backend/tests/test_formats.py` —— 多格式上传端到端（**未创建**）
2. `backend/tests/test_robustness.py` —— 0 字节 / 损坏 PDF / 损坏 docx / 超限 / 解析失败后旧索引可用（**未创建**）
3. `backend/tests/test_desktop_entry.py` —— 桌面入口自动化（**未创建**；只有手工烟测）
4. 缺陷 1（老库缺列）的补丁 —— **未改**
5. 缺陷 2（worker 状态语义）的补丁 —— **未改**
6. `demo_data/fixtures/` 缺 `.pptx` 和 `.md` 两个格式 —— **未补**（用 python-pptx 生成 `产品介绍.pptx`，并给 `scripts/make_fixtures.py` 加 `make_pptx()` / `make_md()`；不要重跑整个脚本覆盖已有 6 个文件）

### 三、两个真实缺陷（关键，下次直接照做）

#### 缺陷 1：老库 `chunks` 表缺 `kind` / `meta` → 任何分块查询 500

**现象**：仓库里 `data/db/app.sqlite3`（16:44 生成）的 `chunks` 表列是
`id, kb_id, doc_id, doc_version_id, ordinal, text, heading, locator, keywords, vector_blob, dim, embed_signature, chunk_hash, created_at`，
**没有 `kind`、`meta`**。实测：

```
OperationalError: no such column: chunks.kind
[SQL: SELECT chunks.id, chunks.kind FROM chunks LIMIT ? OFFSET ?]
```

受影响接口（全部会 500）：`GET /api/documents/{id}/chunks`、`POST /api/search/preview`、
`GET /api/kbs`（`serializers.kb_out` 统计 chunk 数）、`/api/tasks` 之外的多数检索路径。

**为什么 `create_all` 不管用**：`backend/app/db.py:60-62` 的 `init_db()` 只调
`Base.metadata.create_all(bind=engine)`。SQLAlchemy 的 `create_all` 语义是
**「表不存在则建表」**，对已存在的表**完全不发 ALTER**。所以升级到带 `kind`/`meta`
的版本后，用户 AppData 里的老库永远缺列 —— 桌面版必然踩到，开发机因为测试库每次新建反而看不出来。

**修复位置与写法**：改 `backend/app/db.py` 的 `init_db()`，在 `create_all` 之后加一段
「SQLite 缺列自动补齐」：

```python
def _ensure_sqlite_columns() -> None:
    """SQLite 轻量迁移：create_all 只建表不改表，老库升级后新列不存在会直接 500。

    单机部署没有 Alembic，这里只做「加列」这一种安全变更：
    新列一律可空或带默认值，不动既有数据，失败也不阻断启动（下次启动会重试）。
    """
    if not config.DATABASE_URL.startswith("sqlite"):
        return
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(engine.dialect)}'
                default = getattr(col, "default", None)
                if default is not None and getattr(default, "is_scalar", False):
                    ddl += f" DEFAULT {default.arg!r}"   # 老行补齐默认值
                elif not col.nullable:
                    ddl += " DEFAULT NULL"               # SQLite 加 NOT NULL 列必须带默认值
                conn.execute(text(ddl))
```

然后 `init_db()` 改成：

```python
def init_db() -> None:
    from . import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _ensure_sqlite_columns()
```

**要点**：
- 必须放在 `create_all` **之后**，否则新库建表时就重复加列；
- 只做 ADD COLUMN，不做 DROP / 改类型（安全、可重入）；
- `chunks.kind` / `chunks.meta` 都有 `default`，老行会被填成 `'text'` / `'{}'`，
  语义正好是「历史数据都是正文块」，与预期一致；
- 验证方式：`cp data/db/app.sqlite3 /tmp/x.sqlite3` → 指 `WRK_DB_PATH` 到副本 → `init_db()` → 再查 `Chunk` 不报错。
  探针脚本已留在 `.tmp-check/schema_diff.py`（对比模型与实际列）和 `.tmp-check/probe_fixtures.py`。

#### 缺陷 2：解析失败时知识库状态被置 `failed`，但旧索引其实还能用

**位置**：`backend/app/services/worker.py:107-112`

```python
if task and task.kb_id:
    kb = db.get(models.KnowledgeBase, task.kb_id)
    if kb:
        kb.status = models.KbStatus.failed.value   # ← 无条件置 failed
        kb.status_detail = err[:500]
```

**问题**：`ingest.py:109-117` 的设计是「新版本成功入库后才下线旧版本分块」，
所以解析失败时**旧版本分块仍在库里、检索仍能命中**。但这里把 `kb.status` 直接置成
`failed`，前端会显示「失败」，与事实不符，用户会以为整个库不可用。

**修复思路**：置状态前先查该库是否还有分块：

```python
has_chunks = db.execute(
    select(func.count(models.Chunk.id)).where(models.Chunk.kb_id == kb.id)
).scalar() or 0
if has_chunks:
    kb.status = models.KbStatus.partial.value
    kb.status_detail = f"新版本解析失败，旧版本索引仍可用（{has_chunks} 个索引单元）：{err[:300]}"
else:
    kb.status = models.KbStatus.failed.value
    kb.status_detail = err[:500]
```

（`worker.py` 顶部需补 `from sqlalchemy import func, select, update`，当前只 import 了 `select, update`。）
`doc.status` 保持 `failed` 是对的 —— 那个具体文档确实失败了，要区分「文档级」和「库级」。

### 四、下次继续的第一步

```bash
cd F:/GItHub_Code/Wiki-Rag-Knowledge/backend && PYTHONIOENCODING=utf-8 ../.venv/Scripts/python.exe -m pytest tests/ -q
```

确认仍是全绿（暂停时为 82 passed / 1 skipped），然后按顺序：
1. 改 `backend/app/db.py` 加 `_ensure_sqlite_columns()`（缺陷 1）
2. 改 `backend/app/services/worker.py` 的 kb 状态分支（缺陷 2）
3. 补 `demo_data/fixtures/产品介绍.pptx` + `员工手册.md`
4. 写 `backend/tests/test_formats.py` / `test_robustness.py` / `test_desktop_entry.py`
5. 再跑一次全量，确认基线只增不减

### 五、协作边界（避免冲突）

- `backend/app/services/chunking.py` 的 `split_table()` 与 `config.TABLE_CHUNK_BUDGET`：**归 table-chunk-tester**，本次未动，下次也不要动。
- `backend/app/services/parser.py` 的纯文本/PDF 章节标题识别（`_CN_HEADING` 等）：**归 table-chunk-tester**，本次未动。
- `backend/tests/test_tables.py`、`backend/scripts/inspect_chunks.py`：**归 table-chunk-tester**。
- 本任务只写：`backend/tests/test_formats.py`、`test_robustness.py`、`test_desktop_entry.py`、`backend/app/db.py`、`backend/app/services/worker.py`。
- 硬约束：不改 `frontend/**`；不改 `retrieval.py` / `wiki.py` / `chunking.py` 核心算法；不删不跳原有测试。

> **注意（勿误判为回归）**：交接过程中 17:02–17:04 之间曾出现
> `test_tables.py::test_plain_text_sections_do_not_merge` 与
> `test_docx_multi_heading_sections_stay_separate` 两个 FAILED。
> 原因是 table-chunk-tester 当时正在实时修改 `parser.py`（加纯文本章节标题识别）
> 和 `test_tables.py`，属于其开发中间态，不是本项目缺陷。
> 他写完后复跑即为 82 passed / 1 skipped。若下次再看到这两个用例失败，
> 先看 `parser.py` 的 mtime 是否正在变动。

---

## thesis-writer 交接

> 交接时间：2026-10-27 17:05。原因：用户关机，任务暂停。
> **本次没有创建 `docs/DESIGN.md`，也没有创建 `docs/EXPERIMENT.md`**——两者都停在「读代码 + 实测取证」阶段，
> 一个字都没落盘。原因是有意为之：两份文档的硬约束是「所有数字必须来自真实代码或标注待实测」，
> 先取证再动笔，避免写完整稿后再回头改数字。
> **本次没有修改任何源码**，探针脚本写在 `/tmp/wrkprobe/`（仓外，不会污染工作区）。

> **⚠️ 本节的数字有「时效性」陷阱，务必先读这条**
> 取证期间（17:02–17:05）`parser.py` / `chunking.py` / `config.py` 正在被其他 teammate 实时修改，
> 我先后测到过两套不同的值（例如 `TABLE_CHUNK_BUDGET` 从无到 1600，
> `客户投诉处理指引.txt` 的 chunk 数从 1 变 3、PDF 从 1 变 5、测试基线从 59 变 89）。
> **本节数字是 17:05 复测的当前值**，每一条都注明了复测方式。
> 下次动笔前请先重跑 §2 的探针确认未再变动；若已变动，以代码为准，不要以本节为准。

### 一、完成度

| 文档 | 状态 | 完成度 |
| --- | --- | --- |
| `docs/DESIGN.md` | **未创建** | 0%（素材已备齐，10 个章节的代码依据全部落实） |
| `docs/EXPERIMENT.md` | **未创建** | 0%（实验设计、指标口径、实测基线数据已备齐） |
| 代码取证 | 完成 | 100%——见下方「实测数据」，全部用当前 HEAD 代码跑出 |

**取证方式**：`parser.parse_document` + `chunking.split_blocks` 直接跑 fixture 与 demo_data，
再灌一个临时 SQLite 跑 `retrieval.retrieve` / `wiki.build_wiki`（`WRK_DATA_DIR` 指向 tempdir，未污染 `data/`）。

### 二、实测数据（可直接写进论文，均为当前代码产出）

#### 2.1 运行参数（`backend/app/config.py`，全部可用环境变量覆盖）

| 参数 | 值 | 环境变量 |
| --- | --- | --- |
| `DEFAULT_CHUNK_SIZE` | 700 | `WRK_CHUNK_SIZE` |
| `DEFAULT_CHUNK_OVERLAP` | 120 | `WRK_CHUNK_OVERLAP` |
| `TABLE_CHUNK_BUDGET` | 1600 | `WRK_TABLE_CHUNK_BUDGET` |
| `RRF_K` | 60 | `WRK_RRF_K` |
| `PER_KB_CANDIDATES` | 8 | `WRK_PER_KB_CANDIDATES` |
| `MAX_EVIDENCE` | 10 | `WRK_MAX_EVIDENCE` |
| `CONTEXT_CHAR_BUDGET` | 9000 | `WRK_CONTEXT_CHAR_BUDGET` |
| `WORKER_CONCURRENCY` | 2 | `WRK_WORKER_CONCURRENCY` |
| `MAX_UPLOAD_MB` | 50 | `WRK_MAX_UPLOAD_MB` |

`ALLOWED_SUFFIXES` = `.txt .md .markdown .log .pdf .docx .xlsx .pptx`（8 种，见不一致点 13）。
`llm.FALLBACK_EMBED_DIM = 256`，`FALLBACK_EMBED_NAME = "local-hash-embedding"`。
`wiki.MAX_TOPICS = 12`，`wiki.CHUNKS_PER_TOPIC = 6`。

#### 2.2 表格分块三种策略对比（`demo_data/fixtures/`，当前代码）

| 文件 | 表格行数 × 列数 | 逐行切分 | 整表一块 | 行组+表头重复（现实现） |
| --- | --- | --- | --- | --- |
| `员工薪酬明细_120行.docx` | 120 × 9 | 120 块，均长 83 | 1 块 / 10201 字符（**14.6×** 正文预算） | **7 块**，12–18 行/块，均 17.1 行，1055–1558 字符 |
| `员工薪酬明细_700行.docx` | 700 × 9 | 700 块，均长 83 | 1 块 / 59403 字符（**84.9×**） | **39 块**，16–18 行/块，均 17.9 行，1385–1560 字符 |
| `员工薪酬明细_200行.xlsx` | 200 × 9 + 6 × 4（2 工作表） | 206 块 | 1 块 / 16979 字符（**24.3×**） | **13 块**（12 + 1），2–18 行/块，193–1564 字符 |

行区间连续、无重叠：`表格 1 第 1-18 行` → `第 19-36 行` → `第 37-54 行`……最后一块封口到 `total_rows`。
`meta` 字段实测：`{"row_start","row_end","total_rows","cols","caption","part","parts"}`。
小表不切：`产品与销售知识.md` 的 5 行表、`员工手册.md` 的 7 行表、`客户支持与运维手册.md` 的 4 行表，
`locator` 均为 `表格 1 全表`。

#### 2.3 各文件分块产出（当前代码）

| 文件 | blocks | 识别表格 | 总 units | 表格 units | 长度 min–max |
| --- | --- | --- | --- | --- | --- |
| `员工薪酬明细_120行.docx` | 6 | 1 | 9 | 7 | 1055–1558 |
| `员工薪酬明细_200行.xlsx` | 2 | 2 | 13 | 13 | 193–1564 |
| `员工薪酬明细_700行.docx` | 6 | 1 | 41 | 39 | 1385–1560 |
| `研发流程规范.docx` | 18 | 0 | 5 | 0 | 26–87 |
| `客户投诉处理指引.txt` | 7 | 0 | 3 | 0 | 39–50 |
| `差旅与采购管理办法.pdf` | 9 | 0 | 5 | 0 | 9–88 |

> `客户投诉处理指引.txt` 与 `差旅与采购管理办法.pdf` 这两个是**变动过**的：早期测得 1 个 chunk，
> 17:05 复测为 3 和 5——`parser.py` 期间新增了「纯文本/PDF 章节标题识别」（`第 N 段` → `heading`）。
> 这正是 3、5 这两个数字偏碎的原因：识别出 `一、受理` 这类标题后强制切块。
> **这两个数字最不稳定，写作时建议只用 docx/xlsx 的表格数据，正文类用 demo_data 的 markdown。**

`demo_data/` 三个 markdown（`seed_demo.py` 建三个库所用的全部资料）：

| 知识库 | mode | 文件 | units | 其中表格 | 字符数 |
| --- | --- | --- | --- | --- | --- |
| 人事制度库 | wiki | `员工手册.md` | 8 | 1 | 1543 |
| 客户支持库 | basic | `客户支持与运维手册.md` | 10 | 1 | 1463 |
| 产品与销售库 | basic | `产品与销售知识.md` | 7 | 1 | 1222 |
| **合计** | | | **25** | 3 | 4228 |

> 关键量级：demo 三库只有 **25 个检索单元**。这个规模对「40–60 题」的问题集来说太薄，
> 实验文档必须先扩料（见第五节），否则统计上没有意义。

#### 2.4 Wiki 离线构建（`员工手册.md`，8 chunks，无模型配置）

```
WikiBuildResult(pages_created=7, pages_updated=0, pages_skipped=0,
                sections=7, evidence_links=8, mode='live', notes=[])
CallStats: calls=0, prompt_tokens=0, completion_tokens=0, mode='offline'
```

7 个页面 = 7 个二级标题（`一-入职与试用期` … `七-离职管理`），每页 1 个 section，均长 244 字符，
`WikiEvidence` 8 条。**离线降级路径 = 按 heading 聚类 + 抽取式拼装，一次模型都没调。**

#### 2.5 检索耗时与路由（离线哈希向量，3 库 / 25 units）

9 条查询，`retrieval.retrieve()` 端到端（17:05 复测）：**min 6.3 ms / P50 6.6 ms / max 11.9 ms**。
路由全部 `used=wiki|chunk`、`fallback=False`（Wiki 库已就绪，未触发回退）。
这是**离线哈希向量 + 内存遍历**的耗时，不含任何网络往返；接入真实 embedding 后必须重测，不能沿用。

#### 2.6 测试基线

`backend/tests/` → **89 passed, 1 skipped, 1 warning**（17:05 复测两次一致）。
早期测得 59 passed，期间其他 teammate 补了 `test_tables.py` 等用例。

> **踩过的坑**：17:03 曾有一次跑到 `2 failed, 28 passed, 54 errors`，
> 原因是 `parser.py` 正在被实时修改、测试库 schema 与代码不同步。
> 判断方法：立刻重跑，若结果回到 89 passed 就是开发中间态；
> 若稳定失败，先看 `backend/app/**` 的 mtime 是否刚变过。

### 三、文档与实现不一致之处（本次最有价值的产出，共 13 条）

按「影响论文表述的严重程度」排序。前 4 条必须在写作时回避或改写，不能照抄注释。

#### 不一致 1（严重）：`DEFAULT_CHUNK_OVERLAP = 120` 实际不生效，块间没有字符级重叠

`chunking.py:162` 写 `overlap = overlap or config.DEFAULT_CHUNK_OVERLAP`——传 0 会被替换成 120。
但 `split_blocks` 内 **7 处 `flush()` 全部显式传 `carry_overlap=False`**（`chunking.py` 内
`carry_overlap=False` 出现 7 次、裸 `flush()` 在 `split_blocks` 里 0 次），
唯一带 overlap 的分支是超长段落（`len > size*1.6`）里的 `acc = acc[-1:]`，
那是**按句重叠**（回退 1 句），与 120 字符无关。

实测（17:05 复测）：同一输入下 `overlap=0` / `overlap=120` / `overlap=300` 三者产出**完全一致**——
段落聚合路径 lens 恒为 `[663, 663, 165]`、块 2 首 20 字符恒为 `'SEG05填充内容填充内容填充内容填充内'`。
三个不同的 overlap 值产出逐字相同的结果，说明该参数对这条路径**完全没有影响**。

（另一条超长段路径 `len > size*1.6` 用 `acc = acc[-1:]`，那是**按句回退 1 句**，与 120 字符无关；
实测 overlap=0 与 120 在该路径下 lens 也都是 `[734, 524]`，块边界一致。）

**写作要求**：不能写「相邻块保留 120 字符重叠」。应写「同一标题内按段落聚合，块边界不重叠；
超长段落按句切分时回退 1 句以避免句子被截断」。若论文需要 overlap 实验，必须先把参数接上。

#### 不一致 2（严重）：`retrieval.py` 模块 docstring 说跨库用 RRF，实际不是

docstring 第 6 行：「3. 跨库：每库先取 top N，再统一按 RRF 排名融合，避免大库占满候选。」
实际 `retrieve()`（`retrieval.py:539-566`）跨库用的是：

```
score = (0.25 + coverage) × (1 / (1 + 0.45 × local_idx)) × balance
balance = 1.0 if local_idx == 0 else 1 / (1 + 0.10 × kb_idx)
```

`rrf_fuse()` 只在**库内**（关键词通道 vs 向量通道）使用。**跨库没有 RRF。**

**写作要求**：第 6 章「检索与融合」必须分两层写——库内 = 加权 RRF；库间 = 覆盖率 × 名次衰减。
这是全文最容易写错的一处，评审若对照代码会立刻发现。

#### 不一致 3（严重）：`coverage` 不对外输出，`/api/search/preview` 拿不到

`Evidence.coverage` 是跨库融合的唯一跨库可比量，但 `Evidence.to_dict()`（`retrieval.py:253-275`）
**没有输出 `coverage`**。`/api/search/preview` 的 `evidence[]` 里没有这个字段。

**影响**：实验里要复算覆盖率（可用 `stats["query_terms"]` + 证据 `text`/`heading` 自行计算），
或给 `to_dict()` 加一行。**不能写「通过 preview 接口读取 coverage」。**

#### 不一致 4（严重）：Wiki 与 Embedding 的模型调用不计入 `CallStats`

`llm.embed()` 的 `stats` 是可选参数，三处调用**没传**：
- `retrieval.py:506` `llm.embed(db, [query])` —— 查询向量化，发生在 `answering.answer()` 创建 `call_stats` **之前**；
- `wiki.py:205` `llm.embed(db, texts)` —— Wiki 章节向量化；
- `routers/wiki.py:154` —— 人工校对后重建章节向量。

只有 `ingest.py:71/186` 传了 `stats=stats`。

**影响**：`usage.calls` / `prompt_tokens` 只是「生成侧」成本，**不含查询向量化，也不含 Wiki 建索引**。
「整理成本」实验若直接读 `usage`，会系统性低估 Wiki 增强的真实开销——而 Wiki 的成本恰恰主要来自
章节向量化 + 页面生成。必须显式说明，或在实验脚本里用代理计数（embedding 批次数 × 批大小）。

#### 不一致 5（中）：`bm25_score()` 是死代码

`retrieval.py:51` 定义了单 term 的 `bm25_score(term, text, k1=1.4, b=0.72, avg_len=120.0)`，
**全项目无任何调用**。`keyword_rank()` 内部自己内联了一套 tf/idf 计算（`k1=1.4, b=0.72` 硬编码在 88 行）。

**写作要求**：可以写「BM25 变体」，但要落到 `keyword_rank()` 的真实行为上：
IDF 按候选语料实时计算、平均长度按语料计算、关键词字段命中 ×1.8、标题命中 +0.9、
覆盖率加成 `×(1 + 0.18×(hits-1))`、命中门槛 `hits≥2 或 存在≥3 字长词`。不要引用 `bm25_score` 的默认 `avg_len=120.0`。

#### 不一致 6（中）：`_unique_slug()` 是死代码，同名主题会撞唯一约束

`wiki.py:178` 定义了 `_unique_slug()`（带 `-2`/`-3` 后缀去重），**从未被调用**。
`build_wiki` 在 269 行直接用 `chunking.slugify(title)`，然后 `scalar_one_or_none()` 查页面。
若两个主题产出同一 title → 同一 slug → 第二次插入触发 `uq_wiki_kb_slug` 唯一约束。

#### 不一致 7（中）：Wiki 离线聚类的编号与 60 条采样不一致（真实缺陷，已实测复现）

`build_wiki`：`sample = chunks[:60]`，上下文和 `_build_context(sample, ids)` 都基于 sample；
但 `_cluster_topics_offline(chunks)` 传入的是**全量** `chunks`，返回的是**全量** 1-based 序号。

库内 chunk 数 > 60 时，离线主题的编号会超出 sample 范围，`_build_context` 的
`if 1 <= idx <= len(chunks)`（此处形参实为 sample）把它们全部丢弃 → `picked` 为空 →
`pages_skipped += 1`，**页面生成不出来**。

**实测复现**（80 个 chunk，前 60 块同一标题「公共章节」占满采样窗，第 61–80 块各自独立标题）：

```
chunk 总数 = 80  sample = chunks[:60]
WikiBuildResult(pages_created=1, pages_updated=0, pages_skipped=11,
                sections=1, evidence_links=6, mode='live', notes=[])
WikiPage 数 = 1
页面: ['公共章节']
```

12 个主题里只生成了 1 页，**11 页被静默丢弃**，`notes` 里一条提示都没有。
demo 三库只有 8/10/7 个 chunk，未触发。**做 Wiki 实验一旦扩料超过 60 块就会踩到。**

> 补充：若 80 块各自独立标题（每块一个新 heading），则 12 个主题都落在 1..60 内，
> 实测 `pages_created=12`、`pages_skipped=0`，看不出问题——**这个 bug 只在「主题分布跨越第 60 块边界」时暴露**，
> 所以扩料后必须专门构造这种语料来验证。

#### 不一致 8（中）：`wiki_ready` 两处口径不同

- `serializers.kb_out()`：`wiki_ready = 存在 status != draft 且 current_version_id 非空的 WikiPage`
- `kbs.py:_index_status()`：`wiki_ready = WikiSection 计数 > 0`

前者按页面、后者按章节。页面已发布但章节切不出来（正文不足 8 字符）时两者会矛盾。
`plan_route()` 用的又是第三种判定（与 `kb_out` 同口径）。

#### 不一致 9（中）：`top_k` 语义被静默改写

`retrieval.retrieve(top_k=N)`：N 同时作为「每库候选数」和「最终条数」，但最终条数被
`total = MAX_EVIDENCE if top_k is None else max(top_k, 3)` 兜底——**传 `top_k=1` 实际返回 3 条**。
`AskIn.top_k` 在 `docs/API.md` 里没有说明这一下限。

#### 不一致 10（中）：`IndexVersion` 只写不读

模型定义、三处 `db.add(IndexVersion(...))` 都在，但**全项目没有任何查询使用它**
（`grep IndexVersion` 只有 add 和 `seed_demo` 的 delete）。
论文若写「索引版本管理」，须说明当前仅作审计留痕，未参与检索决策或回滚。

#### 不一致 11（轻）：`docs/API.md` 的响应样例缺字段

| 接口 | API.md 写的 | 实际返回 |
| --- | --- | --- |
| `POST /conversations/{id}/ask` | `{message, evidence, route, usage}` + `insufficient` | 另有 `citations: {cited, invalid, citation_ok}` 与 `denied: []` |
| `POST /search/preview` | `{evidence, routes, stats}` | 另有 `denied: []` 与 `context_preview`（完整上下文全文） |
| `GET /api/meta` | `{app_name, initialized, version, offline_models}` | 另有 `capabilities: {llm_configured, embedding_configured, worker_enabled}` |
| `GET /api/kbs`（列表） | 结构示例里带 `index` | `index` 只有 `GET /api/kbs/{id}` 才有 |

#### 不一致 12（轻）：`parser` 支持但上传白名单不支持的格式

`parser.PLAIN_SUFFIXES` 含 `.csv` / `.json`，`HTML_SUFFIXES` 含 `.html` / `.htm`，
但 `config.ALLOWED_SUFFIXES` 不含这些 → 这几条解析分支在**正常上传路径下不可达**
（只有 `seed_demo` 或直接调 `parser.parse_document` 能碰到）。

**写作要求**：第 4 章「多格式解析」的格式清单按 `ALLOWED_SUFFIXES` 的 8 种写，
CSV/JSON/HTML 只能写成「解析层已实现但未开放上传」。

#### 不一致 13（轻）：源码与测试里的密码是脱敏占位符

`security.py` 文档字符串第 3–5 行、`seed_demo.py:23` `DEMO_PASSWORD`、
`test_permissions.py` 的多个密码，都是 `{{PASSWORD_xxx}}` / `{{SECRET_xxx}}` 形式。
它们是**字面量字符串**，能正常当密码用（所以 59 passed 不受影响），但显然被脱敏脚本替换过。
复现实验时若照抄论文里的账号密码会显得可疑，建议在实验文档里写成
「演示账号密码由 `seed_demo.py` 的 `DEMO_PASSWORD` 常量决定」。

### 四、另外两条与论文结论直接相关的实测发现

这两条不是「文档与实现不一致」，而是**实测行为与测试断言不一致**，会直接影响实验结论的写法。

#### 发现 A：无关查询在真实语料上仍会返回高覆盖率证据

`test_retrieval_quality.py::test_no_result_for_unrelated_query` 断言
「完全不相关的查询不应产生有命中的证据」，但它的断言写的是
`if evidence: assert all(e.coverage == 0 ...)`——**只在自己那个 2 库小语料上成立**。

实测 demo 三库 + 查询「量子计算机的退相干时间是多少」：返回 **7 条证据**，
首位 `coverage=0.08`（命中「时间」类二元组），并非 0。

**结论**：覆盖率门槛（`coverage > 0` 只用于排序让位，不是过滤）**不能作为拒答判据**。
「无答案题」的正确率只能靠生成侧的 `REFUSAL_MARKERS` + `insufficient` 判定，
而那个判定是 `(not cited) and any(marker in text)`——**只要模型带任意合法引用编号就不会判 insufficient**。
实验文档的「无答案题」指标必须人工判定，不能依赖接口字段。

#### 发现 B：覆盖率高的证据可能排在后面（名次衰减压过覆盖率）

查询「P0 工单首次响应时限」实测：

| rank | 来源 | coverage | 内容 |
| --- | --- | --- | --- |
| 1 | chunk | 0.38 | 「支持渠道与响应时效」正文段落 |
| 2 | chunk | **0.62** | 工单分级表格（含 15 分钟答案） |

coverage 0.62 > 0.38，但 rank 更低——因为两者同属一个库，`rank_term = 1/(1+0.45×local_idx)`
的库内名次衰减（第 1 名 1.0、第 2 名 0.690）压过了覆盖率差（0.87 vs 0.63）。
这是**设计使然**（库内名次由 RRF 决定，跨库只做粗排），但意味着：
**「证据覆盖率」作为检索指标时，与最终 rank 不是单调关系**，不能把两者混为一谈。
第 4 章实验四、第 5 章指标定义要分别说清「库内名次」和「跨库覆盖率」两个层次。

### 五、下次继续的第一步

不要重新读全部代码——上面的实测数据和不一致清单已经够写 80% 的正文。直接：

```bash
cd F:/GItHub_Code/Wiki-Rag-Knowledge
cat docs/HANDOFF.md                              # 先看本节
ls /tmp/wrkprobe/                                # 探针脚本（仓外，可能已被系统清理）
cd backend && PYTHONIOENCODING=utf-8 ../.venv/Scripts/python.exe -m pytest tests/ -q   # 确认基线
```

探针脚本若已被清理，按 §2 的描述重建即可（都是几十行：`parse_document` → `split_blocks` → 统计；
灌临时 SQLite → `retrieve` / `build_wiki`）。**核心是 `WRK_DATA_DIR` / `WRK_DB_PATH` 指向 tempdir，不要写 `data/`。**

1. **先写 `docs/DESIGN.md`**（10 节，3000–4000 字）。第 6 节「检索与融合」按不一致 2 分两层写；
   第 5 节「分块策略」用 2.2 的三策略对比表，overlap 按不一致 1 的措辞写。
2. **再写 `docs/EXPERIMENT.md`**。四个对比实验的骨架：
   - 实验一：`mode=basic` vs `mode=wiki` 同资料同题集
   - 实验二：单库 vs 多库（用 `kb_ids` 单元素 vs 三元素）
   - 实验三：加权 RRF vs 等权 vs 纯关键词 vs 纯向量（消融；等权可调 `channel_weights`，
     纯关键词令 `qvec=None`，纯向量置 `weights=[0,1]`——注意后两者需要改代码或加开关，当前无接口）
   - 实验四：表格三策略（逐行 / 整表 / 行组）——用 2.2 的数据，需要临时改 `split_table` 或在探针脚本里复现
3. **扩料**（硬前提）：demo 三库仅 25 个检索单元，撑不起 40–60 题。建议把
   `demo_data/fixtures/` 的 6 个文件也入库（700 行表格库能一次提供 39 个表格块），
   并补 3–5 篇同领域长文档，把总量做到 **150–300 块**。
   **注意**：扩到 60 块以上会触发不一致 7 的 Wiki 丢页 bug，先修或先用非 Wiki 模式做实验。
4. 成本指标必须自己埋点（不一致 4）：在 `retrieval.py:506`、`wiki.py:205` 传 `stats`，
   或在实验脚本里包一层 `llm.embed` 计数。

### 六、协作边界

- 本次**未改任何源码**，探针脚本全在 `/tmp/wrkprobe/`（仓外）。
  未碰 `backend/**`、`frontend/**`、`docs/API.md`。
- 下次若要给 `Evidence.to_dict()` 加 `coverage`（不一致 3）或给三处 `llm.embed` 补 `stats`（不一致 4），
  属于**改源码**，需先与 backend-integrator 协调，避免与其缺陷 1/2 的补丁冲突。
- `docs/DESIGN.md`、`docs/EXPERIMENT.md`、`docs/HANDOFF.md` 归 thesis-writer。
- **协作提醒**：取证期间 `parser.py` / `chunking.py` / `config.py` 被其他 teammate 实时改动，
  导致我两次测到不同数值。下次开工前请先 `git status` / 看 mtime，确认代码已静止再取数。

---

## frontend-dev 交接

> 交接时间：2026-10-27 17:0x。原因：用户关机，暂停。
> **构建状态：`tsc -b --force` → 0 错误；`vite build` → 成功**（dist 51 modules，约 348 kB JS / 32.7 kB CSS）。
> 已尊重 Lead 的桌面化改动（`initApiBase`/`apiUrl`/`isDesktop`、`HashRouter`、`base:'./'`、`electron/**`），未回退任何一处。

### 一、七个页面完成度

| # | 路由 | 完成度 | 已接的交互 |
| --- | --- | --- | --- |
| 1 | `/login` | 100% | 用户名/密码登录、错误中文提示、`must_change_password` 强制改密弹窗（不可 Esc 关闭）、改密后 `refreshMe` 再跳转、`RequireAuth` 对未改密用户二次拦截。**已用真实账号端到端跑通**：首次登录弹窗 → 改密 → 落到 `/kbs` |
| 2 | `/kbs` | 100% | 卡片/表格双视图、`mode_label`/`status_label`/文档数/分块数/Wiki 页数/`my_level`、admin「新建知识库」（名称/描述/mode）、点击进详情 |
| 3 | `/kbs/:id` | 95% | 文档 Tab：多选+拖拽上传、逐文件移除、上传错误按 `{filename,error}` 展开、列表（文件名/版本/状态/分块/大小/时间）、下载（blob）、上传新版本、删除（确认弹窗）、分块预览（分页 10 条，含 locator/heading/维度/关键词）、本库任务列表（进度条/阶段/失败原因/重试）。Wiki Tab：目录树+状态点+锁定标记、Markdown 渲染、来源依据面板、关联页面、编辑校对（Markdown textarea → PUT）、`human_locked` 标记与解除锁定、版本历史列表。设置 Tab：改名/描述/切 mode、重建分块索引、重建 Wiki（basic 模式下按钮禁用并说明原因）、`embed_signature`/维度/时间展示、最近任务 |
| 4 | `/chat`、`/chat/:conversationId` | 95% | 会话列表（新建/重命名/删除）、知识库多选（只列可见库，带 manage/只读角标）、消息流、loading 骨架、证据卡片（折叠展开原文、doc_name/版本/locator/heading/score/channels）、路由面板（每库 used/reason/fallback/hits + 全局 stats）、耗时与模型调用统计、`insufficient` 醒目提示、Wiki 证据「查看 Wiki 页面」跳 `/kbs/:id?tab=wiki&slug=`、`denied` 库 toast |
| 5 | `/admin/users` | 100% | 列表（用户名/显示名/角色/状态/授权数）、新建用户（含授权勾选 + read/manage）、编辑显示名/角色/状态、重置密码、编辑授权（全量替换）、停用（不能停自己）、非 admin 路由拦截 + 菜单隐藏 |
| 6 | `/admin/models` | 92% | 供应商 CRUD（Key 输入框留空=不改，显示 `api_key_masked`）、模型 CRUD 按 llm/embedding/vision 分组、设默认、启用开关、连接测试（显示结果+延迟）、Embedding 区（当前签名/各库一致性/需 confirm 的全量重建二次确认） |
| 7 | `/tasks` | 100% | 全部任务、状态/知识库筛选、进行中计数、**2 秒轮询（无运行中任务自动停，实测 6 秒内只有 2 次请求）**、失败/部分失败重试、组件卸载清理 |

### 二、已知未完成项（按优先级）

1. **Wiki 版本历史看不到历史正文**。后端 `GET /kbs/{id}/wiki/pages/{slug}/versions` 只返回 `{id,version_no,origin,note,is_current,chars,created_at}`，**没有 `content_md`**。前端已做兜底（点「查看内容」时若无正文则给空态提示），要真正可看需后端补字段。
2. **会话的知识库选择不回写**。只在 `POST /ask` 时用 `kb_ids` 覆盖，没有调 `PATCH /conversations/{id}`；换会话时会用会话自带的 `kb_ids`。想让选择持久化，在 `ChatPage` 的 `toggleKb` 里补一次 PATCH 即可。
3. **历史消息没有 `insufficient` / `usage`**。`message_out` 不落这两个字段，前端用「无证据且有 route」近似判定「证据不足」。要精确需后端在 `Message` 上落 `insufficient`。
4. **供应商 API Key 清空**：后端支持传 `"__clear__"`，前端没做「清空 Key」按钮（只有留空=不改）。
5. **模型 `params`（JSON）没有编辑 UI**，编辑时原样带回。
6. `DELETE /api/kbs/{id}`（删知识库）、`GET /api/settings/audit`（审计日志）、`POST /api/search/preview`（检索预览）三个接口 **已在 `api.ts` 封装但没接页面**。
7. 表格视图/任务列表没做虚拟滚动，几千条会卡；移动端（<820px）侧边栏抽屉只做了基础样式，未实测。
8. **HashRouter 改造后未复测页面**：我的全部 UI 验收是在 Lead 切 `HashRouter` **之前**用 history URL（`/chat`、`/kbs/1?tab=wiki`）做的。切 Router 后深链要写 `#/chat`、`#/kbs/1?tab=wiki`，`?tab=` 在 hash 内仍由 `useSearchParams` 解析。**下次必须先按新 URL 形式重跑一遍**。

### 三、接口归一化（后端实现与 `docs/API.md` 有出入，已在 `api.ts` 吸收，页面代码不用改）

| 接口 | 后端实际返回 | `api.ts` 归一化 |
| --- | --- | --- |
| `GET /kbs/{id}/wiki/tree` | `{items:[...]}` | `tree()` 补出 `pages` |
| `GET /kbs/{id}/wiki/pages/{slug}` | `page` 字段平铺在顶层 + `version/evidence/related` | `page()` 组装成 `{page, version, evidence, related}` |
| `GET /settings/embedding/status` | `{active:{...}, kbs:[{mismatch}], has_index, running_tasks}` | 拍平成 `current_signature/consistent/rebuilding` |
| `POST /settings/embedding/switch` | `{ok, rebuild_required, tasks:[{task_id}], message}` | `task_id` 取 `tasks[0]` |
| `POST /kbs/{id}/documents` | `errors:[{filename,error}]` | 类型改对象，UI 按「文件：原因」展示 |
| `GET /documents/{id}/chunks` | `dim` / `keywords[]` / `has_vector` | 类型补齐，预览优先读 `dim` |

### 四、下次继续的第一步

```bash
cd F:/GItHub_Code/Wiki-Rag-Knowledge/frontend
node "C:/Users/Administrator/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/pnpm/bin/pnpm.mjs" exec tsc -b --force   # 应为 0 错误
node "C:/Users/Administrator/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/pnpm/bin/pnpm.mjs" exec vite build      # 应成功
```

然后起后端 8000 + `pnpm dev:web` 5173，**用 HashRouter 的新 URL 形式**复测：

- `http://127.0.0.1:5173/#/login` → 登录 → `#/kbs`
- `#/kbs/1?tab=documents|wiki|settings`（注意 `?tab=` 在 `#` 之后）
- `#/chat`、`#/chat/1`
- `#/tasks`、`#/admin/users`、`#/admin/models`

要恢复的测试账号（本次验收时建的，可直接删）：用户 `pwdtest`（已改过密码）、知识库「空库测试」(id=4)。

### 五、协作边界

- 我写的文件：`frontend/src/{main,App,api,auth,types,utils,styles}.ts(x)`、`frontend/src/pages/*`、`frontend/src/components/*`、`frontend/{package.json,vite.config.ts,tsconfig*.json,index.html,pnpm-workspace.yaml}`。
- **不要动**：`frontend/electron/**`（桌面打包）、`frontend/scripts/**`（build-backend 等）、`frontend/ui_check.mjs`（Lead 的检查脚本）。
- 我加的 `pnpm-workspace.yaml` 只有 `onlyBuiltDependencies: [esbuild]` + `verifyDepsBeforeRun: false`，用于消掉 pnpm 的 `ERR_PNPM_IGNORED_BUILDS` 噪声；若桌面打包需要 workspace 语义，请确认后再改。

---

## 表格分块验证与加固（table-chunk-tester 交接）

> 时间：2026-10-27 17:05
> 状态：**已完成并通过全量测试**（`82 passed, 1 skipped`）

### 1. 本次新增/修改的文件

| 文件 | 性质 | 说明 |
| --- | --- | --- |
| `backend/tests/test_tables.py` | 新增 | 表格分块与解析回归测试，23 个用例（含 1 个参数化 ×4） |
| `backend/scripts/inspect_chunks.py` | 新增 | 实测脚本，打印每个 fixture 的块数/表格数/块长/行区间，有超长块时退出码 1 |
| `backend/app/services/parser.py` | 修补 | 纯文本章节标题识别（`looks_like_plain_heading`）；同时删掉我重复定义的 `_is_plain_heading` 死代码 |
| `backend/app/services/chunking.py` | 未改 | 现状已满足要求，未做修改 |
| `backend/app/config.py` | 未改 | `TABLE_CHUNK_BUDGET = 1600` 由 Lead 添加，我只读取 |

**未触碰**：`backend/app/routers/**`、`retrieval.py`、`wiki.py`、`frontend/**`、原有 59 个测试。

### 2. 实测数字（`inspect_chunks.py` 真实输出）

表格块预算 `TABLE_CHUNK_BUDGET=1600`，正文预算 `DEFAULT_CHUNK_SIZE=700`（安全上限 910）。

| fixture | 解析块 | 表格 | 分块 | 表格块 | 字符 min/avg/max | 行区间 |
| --- | --- | --- | --- | --- | --- | --- |
| `员工薪酬明细_700行.docx` | 6 | 1 | 41 | **39** | 1385 / 1552 / **1560** | (1,18) … (685,700) 连续无遗漏 |
| `员工薪酬明细_120行.docx` | 6 | 1 | 9 | **7** | 1055 / 1483 / 1558 | (1,18) … (109,120) 连续无遗漏 |
| `员工薪酬明细_200行.xlsx` | 2 | 2 | 13 | **12 + 1** | 193 / 1446 / 1564 | 薪酬明细 (1,18)…(199,200)；部门汇总整表 1 块 |
| `客户投诉处理指引.txt` | 7 | 0 | 3 | 0 | — | 3 个章节各自成块 |
| `差旅与采购管理办法.pdf` | 9 | 0 | 5 | 0 | — | 文本块带页码 locator |
| `研发流程规范.docx` | 18 | 0 | 5 | 0 | — | 4 个章节 + 引言 |

**700 行表格明细**：切 39 块，平均 17.9 行/块，首块 `表格 1 第 1-18 行`（1557 字符），末块 `表格 1 第 685-700 行`（1385 字符）。每块都含「工号」「月薪」表头，行区间拼起来正好是 1..700。全部块长度安全，**无超长块**。

### 3. 发现的真实缺陷与修复

**缺陷 1（真实缺陷，已修）**：纯文本 `.txt` 的章节标题不被识别。

- 现象：`客户投诉处理指引.txt` 的「一、受理」「二、处理」「三、回访」全被当成正文段落，3 个章节 + 全文标题被并进**同一个块**（159 字符 1 块）。检索命中后分不清是哪一条流程。
- 根因：`_parse_plain()` 只认 Markdown 的 `#` 标题，`.txt` 没有任何 heading 判定分支，`heading` 恒为空，`split_blocks()` 的「标题变化强制切块」逻辑永远不会触发。
- 修复：`parser.py` 新增 `looks_like_plain_heading()`，用「编号前缀 + 行短 + 不以句末标点结尾」三条启发式识别，排除 `.csv`/`.json`。
- 效果：该文件从 1 块变成 3 块，每块带正确章节标题。

**缺陷 2（已在过程中被修）**：`chunking.py` 曾引用不存在的 `config.TABLE_CHUNK_BUDGET`，导致 `AttributeError` 直接崩溃。现 `config.py` 已定义该常量，功能正常。

**未修复的已知限制（见下）**：单行超长会超预算。

### 4. 测试覆盖清单（`backend/tests/test_tables.py`）

- 700 行 Word：块数 > 10、每块含表头字段、块长 ≤ `TABLE_CHUNK_BUDGET`、行区间拼起来 = 1..700
- 120 行 Word：行区间完整、预算与表头
- 200 行 Excel：两个工作表各自独立成表、`heading` 为工作表名、locator 带工作表名、行区间完整
- 小表（2/6/12/24 行）：整表一块不切分、locator 用「全表」
- locator 格式：`表格 2 第 21-40 行` 正则校验 + 与 meta 一致 + 块首行内容确实来自 `row_start` 那一行（真正定位回去，不只是字符串匹配）
- meta：`row_start`/`row_end`/`total_rows`/`cols` 齐全，`part`/`parts` 编号连续
- PDF：文本块带页码；表格块（若识别到）locator 带页码
- 图片：`parsed.images` 非空、**无 image 内容块**、monkeypatch `llm.describe_image` 断言 0 次调用、图片前后正文都保留
- 纯文本 / Markdown / 多级标题 docx：标题变化强制切块，同一块不混两个章节

### 5. 已知限制（下次可继续）

1. **单行超预算**：某一行单元格内容极长（>1600 字符）时，该行单独成块仍会超预算（实测 2095 字符）。当前实现是「行是原子的，不拆行」。若要彻底解决需按单元格截断，但会破坏行的完整性，需要产品决策。
2. **PDF 表格识别是启发式的**：靠「2+ 空格或 `|` 对齐 + 列数一致」判断。用 reportlab `STSong-Light` 生成的中文表格能识别；用 `Courier` 等无中文字形的字体时，中文会被提取成 `■` 方框，此时**仍能识别为表格但列名是乱码**。扫描件 PDF 直接 `ParseError`（需 OCR）。
3. **PDF 表格测试会 skip**：`test_pdf_table_locator_contains_page_number` 在识别不到表格时 `pytest.skip`（启发式允许漏识别），这是有意的，不是漏测。
4. **图片只记元信息**：`parsed.images[].data` 恒为 `b""`，不读入内存。视觉模型/OCR 接入前，图片内容不进索引。
5. **小表用 Markdown 渲染**（≤25 行），大表用「列名: 值」渲染——两种格式的块长不可直接横向比较。

### 6. 下次继续的第一步

```bash
cd F:/GItHub_Code/Wiki-Rag-Knowledge/backend
PYTHONIOENCODING=utf-8 ../.venv/Scripts/python.exe -m pytest tests/ -q      # 应 82 passed, 1 skipped
PYTHONIOENCODING=utf-8 ../.venv/Scripts/python.exe -m pytest tests/test_tables.py -v   # 只看表格用例

# 实测数字
cd F:/GItHub_Code/Wiki-Rag-Knowledge
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe backend/scripts/inspect_chunks.py
```

### 7. 协作边界

- 我独占：`backend/tests/test_tables.py`、`backend/scripts/inspect_chunks.py`
- 我改过：`backend/app/services/parser.py`（仅新增 `looks_like_plain_heading` 相关，无其它改动）
- `backend-integrator` 已确认不碰 `split_table()` / `TABLE_CHUNK_BUDGET` / 这两个文件

---

## desktop-packager 交接

> 交接时间：2026-10-27 17:05。原因：用户关机，任务暂停。
> **本次没有修改任何 `backend/app/**`、`frontend/src/**`、`frontend/electron/main.js`、`frontend/electron/preload.cjs`。**

### 一、已产出的文件

| 路径 | 用途 | 状态 |
| --- | --- | --- |
| `frontend/scripts/build-backend.mjs` | PyInstaller 把 `backend/app/desktop_entry.py` 打成单文件 `build/backend/wrk-backend.exe`，自带启动 + `/api/health` 实测 | ✅ 已跑通，exe 已产出 |
| `frontend/scripts/build.mjs` | 编排 `tsc -b` → `vite build` → build-backend → electron-builder；`--pack` 走 `--dir` | ✅ 语法/逻辑完成，**未跑过完整流程** |
| `frontend/scripts/dev.mjs` | 起 vite(5173) → 等就绪 → 以 `WRK_DEV=1 WRK_DEV_URL=...` 起 Electron | ✅ 语法完成，**未实跑** |
| `frontend/electron-builder.yml` | appId / productName / win nsis / extraResources / files / NSIS 选项 | ✅ YAML 已校验可解析，**未跑过 electron-builder** |

产物实测：

```
frontend/build/backend/wrk-backend.exe   56,641,083 字节（54.0 MiB）
```

### 二、已验证的部分（验收标准 1 —— 通过）

打包后的 exe 独立启动成功，真实输出：

```
INFO:     Started server process [35908]
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:54327 (Press CTRL+C to quit)
WRK_READY {"host": "127.0.0.1", "port": 54327, "version": "1.0.0", "app": "企业知识库文档整理与问答系统", "pid": 35908}
```

HTTP 实测：

```bash
curl -s http://127.0.0.1:54327/api/health
# {"ok":true,"version":"1.0.0"}

curl -s http://127.0.0.1:54327/api/meta
# {"app_name":"企业知识库文档整理与问答系统","version":"1.0.0","initialized":true,
#  "offline_models":true,"capabilities":{"llm_configured":false,"embedding_configured":false,"worker_enabled":true}}
```

`/api/meta` 能通说明 SQLAlchemy + sqlite 方言 + 建表 + 管理员 bootstrap 全部打进 exe 了，
不只是 FastAPI 空壳能起来。

> 注意：控制台里中文显示成乱码是 Git Bash 的 cp936 解码问题，不是程序缺陷 ——
> `WRK_READY` 那行 JSON 里的中文是正确的。

### 三、PyInstaller 打后端的坑（重点，下次直接照做）

**坑 1：`desktop_entry.py` 不能直接当脚本打包（最容易踩）**

它内部是相对导入 `from .main import app, APP_NAME, VERSION`。
把 `.py` 路径直接喂给 PyInstaller，入口模块的 `__name__` 是 `__main__`、没有 parent package，
运行时必报：

```
ImportError: attempted relative import with no known parent package
```

解法（脚本里已实现）：生成一个 4 行 launcher 再打包，并用 `--paths backend` 把 `app` 包暴露给分析器。

```python
# frontend/build/pyinstaller/wrk_launcher.py（build-backend.mjs 自动生成）
import sys
from app.desktop_entry import main
if __name__ == "__main__":
    sys.exit(main())
```

```bash
python -m PyInstaller --onefile --paths backend ... wrk_launcher.py
```

**坑 2：必须 `--console`，不能 `--windowed`**

Electron 主进程靠 `child.stdout` 上的 `WRK_READY {json}` 拿动态端口。
`--windowed`（GUI 子系统）下 stdout 是空的，主进程会一直等到 60s 超时然后弹「后端服务未就绪」。

**坑 3：`--collect-all` 清单（脚本里的 `COLLECT_ALL` 常量，附原因）**

| 包 | 为什么必须 collect-all / hidden-import |
| --- | --- |
| `pypdf` | 解析 PDF。内部按版本分支动态 import 子模块，静态分析扫不到 |
| `docx` (python-docx) | 需要 `docx/templates/default.docx` 作为新建文档模板（数据文件，不是 .py） |
| `pptx` (python-pptx) | 需要 `pptx/templates/default.pptx` + 主题/版式资源 |
| `openpyxl` | 需要 `.constants.json` 之类元数据文件，缺了读 xlsx 会静默失败 |
| `lxml` | C 扩展 + `isoschematron` 的 xsl 资源；hook 覆盖不全时 xpath 引擎缺失 |
| `cryptography` | `security.py` 的 Fernet 依赖，rust 绑定 + backend 接口需整体收集 |
| `sqlalchemy` | dialect 走 registry 字符串动态加载，`sqlalchemy.dialects.sqlite` 必须 hidden-import |
| `uvicorn` | `loops.auto` / `protocols.http.auto` / `lifespan.on` 全是字符串动态 import |
| `python_multipart` + `multipart` | FastAPI `UploadFile` 依赖，按需 import 时容易漏 |
| `starlette` / `fastapi` | 子模块 + 少量模板资源 |

另外固定加的 `HIDDEN_IMPORTS`：`app.desktop_entry`、`app.main`、
`sqlalchemy.dialects.sqlite.pysqlite`、`encodings.idna`、`email.mime.text`。

**坑 4：验证时 `WRK_WATCH_STDIN` 必须为 `0`**

`desktop_entry.py:79` 有 stdin 看门狗：父进程 stdin EOF 就 `server.should_exit = True`。
用脚本 spawn 验证时如果 `stdio[0]` 给 `'ignore'`，stdin 立刻 EOF，后端刚起来就自己退了。
脚本里验证时显式传 `WRK_WATCH_STDIN: '0'`；真实 Electron 场景保持 `1`（`main.js` 已这么设）。

**坑 5：`--noupx`**

不压壳，避免杀软误报，也避免 onefile 启动更慢（解压 + UPX 二次解压叠加）。

### 四、electron-builder 配置的关键点

`frontend/electron-builder.yml` 里几个容易配错的地方：

1. **`directories.output: release`，不是 `dist`**
   vite 产物就在 `frontend/dist/`。如果 output 也写 `dist`，electron-builder 会往
   `dist/win-unpacked` 里写，虽然能共存，但 `files: dist/**/*` 有把上一次产物递归打进 asar 的风险。
   分开到 `release/` 干净得多。**验收标准里写的 `dist/win-unpacked/` 实际落在 `frontend/release/win-unpacked/`。**

2. **`compression` 是顶层键，不是 `nsis` 的子键**
   写到 `nsis:` 下面会被 schema 校验拒绝（`should NOT have additional property`）。

3. **`extraResources` 的 `from` 是相对 `frontend/` 的**
   `from: build/backend` → `to: backend`，最终落在 `resources/backend/wrk-backend.exe`，
   正好对上 `electron/main.js:48` 的 `join(process.resourcesPath, 'backend', 'wrk-backend.exe')`。

4. **`files` 要显式排除 `release/**`、`build/**`、`scripts/**`、`src/**`**
   否则 `build/backend` 里那个 57MB 的 exe 会被 asar 再打一份（体积翻倍）。

5. **`build/icon.ico` 目前不存在**
   已在 yml 里去掉 `installerIcon` / `installerHeaderIcon` 引用，缺图标时 electron-builder
   会用 Electron 默认图标继续，不会失败。要换图标就把 256×256 的 `icon.ico` 丢到 `frontend/build/icon.ico`。

6. **`asarUnpack: ['**/*.node']`** —— 后端是 extraResources 不进 asar，这条是给未来原生模块留的。

### 五、还没验证的部分（下次必须补）

| 验收项 | 状态 |
| --- | --- |
| 1. `node scripts/build-backend.mjs` 产出 exe + `/api/health` | ✅ **已通过**（见上文实测） |
| 2. `node scripts/build.mjs --pack` 产出 `release/win-unpacked/` 且含 `resources/backend/wrk-backend.exe` | ❌ **完全没跑过** |
| 3. 启动打包后的 `企业知识库.exe`，验证能拉 sidecar、进程不崩 | ❌ **完全没跑过** |
| 4. 报告产物路径与体积 | ⚠️ 只有 sidecar 的（54.0 MiB），应用本体和安装包都没有 |
| `node scripts/dev.mjs` 实跑 | ❌ 只做了 `node --check` 语法校验 |
| electron-builder 首次下载（electron 二进制 / nsis 工具链） | ❌ 未触发，**大概率是下次最大的时间黑洞** |

`node_modules` 已装好：`electron@33.4.11`、`electron-builder@25.1.8`，electron 二进制已下载
（`node_modules/electron/dist/electron.exe`，188,784,128 字节）。但 electron-builder 自己的
winCodeSign / nsis 工具链还没下过，首次跑 `--pack` 会额外下载。

### 六、⚠️ 环境变量陷阱：`ELECTRON_RUN_AS_NODE=1`

**这台机器全局设了 `ELECTRON_RUN_AS_NODE=1`**（实测确认）。

它会让 Electron 退化成纯 Node 进程，后果：

- `ipcMain` 是 `undefined` → `Cannot read properties of undefined (reading 'handle')`
- `app.whenReady()` 不触发，窗口永远不出来
- 整个 `main.js` 从第 291 行 `ipcMain.handle('wrk:info', ...)` 直接崩

**手工启动必须：**

```bash
env -u ELECTRON_RUN_AS_NODE node_modules/.bin/electron .
```

```powershell
$env:ELECTRON_RUN_AS_NODE = $null
node_modules\.bin\electron.cmd .
```

**脚本已处理**：`dev.mjs` 里有 `electronEnv()`、`build.mjs` 里有 `cleanEnv()`，
两者 spawn 前都 `delete env.ELECTRON_RUN_AS_NODE`。下次写新脚本（尤其是验证 `--pack` 产物的脚本）
**也必须照抄这一步**，否则会误判成「应用启动失败」。

### 七、下次继续的第一步

```bash
cd F:/GItHub_Code/Wiki-Rag-Knowledge/frontend

# 0) 确认 exe 还在（已在，54 MiB）
ls -la build/backend/wrk-backend.exe

# 1) 只跑 electron-builder 的 --dir（跳过后端和前端，最快验证配置对不对）
node scripts/build.mjs --pack --skip-backend --skip-web

# 期望产出：
#   frontend/release/win-unpacked/企业知识库.exe
#   frontend/release/win-unpacked/resources/backend/wrk-backend.exe
#   frontend/release/win-unpacked/resources/app.asar

# 2) 若第 1 步通过，跑完整 pack（重新走前端构建）
node scripts/build.mjs --pack

# 3) 启动验证（注意 ELECTRON_RUN_AS_NODE）
cd F:/GItHub_Code/Wiki-Rag-Knowledge/frontend/release/win-unpacked
env -u ELECTRON_RUN_AS_NODE ./企业知识库.exe &
sleep 20
# 应看到 2 个进程：企业知识库.exe（主）+ wrk-backend.exe（sidecar）
powershell -c "Get-Process 企业知识库,wrk-backend -ErrorAction SilentlyContinue | Select Id,ProcessName"

# 4) 验证 sidecar 真的被拉起并监听端口
powershell -c "Get-NetTCPConnection -State Listen | Where-Object { \$_.OwningProcess -in (Get-Process wrk-backend).Id }"

# 5) 最后生成 NSIS 安装包
node scripts/build.mjs
ls -la release/*.exe
```

如果第 1 步 electron-builder 报下载失败（`ERR_ELECTRON_BUILDER_CANNOT_EXECUTE` /
winCodeSign 解压失败），先设镜像再重试：

```bash
export ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/
export ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
```

### 八、协作边界

- 我独占：`frontend/scripts/build-backend.mjs`、`frontend/scripts/build.mjs`、
  `frontend/scripts/dev.mjs`、`frontend/electron-builder.yml`
- 我**没有**改：`backend/app/**`、`frontend/src/**`、`frontend/electron/main.js`、
  `frontend/electron/preload.cjs`、`frontend/package.json`、`frontend/vite.config.ts`
- `package.json` 里已声明好 `build:backend` / `build` / `pack` / `dev` 四个 script，
  指向的正是上面三个 .mjs，**不需要再改 package.json**
- 我往项目 `.venv` 装了 `pyinstaller 6.22.3`（连带 altgraph / pefile / pyinstaller-hooks-contrib /
  pywin32-ctypes / setuptools），以及 `frontend/node_modules` 里的 electron + electron-builder

---

## frontend-dev 交接 · 二期（P1/P2 全部完成）

> 交接时间：2026-10-27 18:2x。
> **构建：`tsc -b --force` → 0 错误；`vite build` → 成功（57 modules，JS 386 kB / CSS 37.7 kB）。**
> 未改 Lead 的 `initApiBase`/`apiUrl`/`isDesktop`、`main.tsx` 的 HashRouter、`frontend/electron/**`。

### 一、P1 六项（全部完成，逐项真机验证）

| 项 | 实现位置 | 验证结果 |
| --- | --- | --- |
| P1-1 `DELETE /kbs/{id}` | `pages/KbListPage.tsx`：卡片底部 + 表格行「删除」，仅 admin 渲染；`ConfirmDialog` 文案含「将同时删除该库的 N 个文档、M 个分块索引、K 个 Wiki 页面，以及磁盘上的原始文件 / 此操作不可恢复」 | 实测弹窗文本正确；真删 `DELETE /api/kbs/4` → 200，列表 4→3，toast「知识库「待删除验证库」已删除」 |
| P1-2 审计日志 Tab | `pages/ModelsPage.tsx`：顶部「供应商与模型 / 审计日志」两个 Tab，`AuditPanel` 组件（时间/用户/操作/对象/详情 + 首末页与上下页）；`types.ts` 新增 `AUDIT_ACTION_LABEL` 中文动作映射 | 实测 41 条数据分 3 页；翻页请求 `?page=2&page_size=20`；「修改知识库 / 新建知识库 / 修改供应商 / 新建供应商 / 登录」中文标签全部命中 |
| P1-3 检索调试页 | 新增 `pages/SearchPage.tsx` + 路由 `/search` + 侧边栏「检索调试」。展示：概览 6 项指标（候选数/入选数/返回证据/平均覆盖率/RRF k/每库候选上限）、各库入选分布、切分后查询词、路由表（模式/命中通道/是否回退/三通道命中/决策原因）、证据表（覆盖率进度条 + 融合分 + 通道名次 + 来源 locator，可展开原文与 Wiki 原文依据）、拼装上下文预览 | 实测 4 候选/4 入选/覆盖率 0.20/10 个查询词/3 库路由/4 条证据全部渲染；展开原文与上下文均正常 |
| P1-4 会话知识库回写 | `pages/ChatPage.tsx`：`toggleKb` 与「清空选择」改为乐观更新 + `PATCH /conversations/{id}`；失败 toast 但不回滚 | 实测发出 `PATCH /api/conversations/1`，body `{"kb_ids":[2,3]}`，响应 200，UI 同步 |
| P1-5 清空 Key | `pages/ModelsPage.tsx`：编辑供应商弹窗内，仅 `has_key=true` 时显示「清空已保存的 API Key」→ 二次确认 → `PATCH {api_key:"__clear__"}` | 实测 PATCH body 为 `{"api_key":"__clear__"}`，行内 Key 从 `sk-****3456` 变「未配置」 |
| P1-6 params JSON | `pages/ModelsPage.tsx`：模型弹窗新增可折叠「高级参数（JSON）」，带格式化/清空按钮；保存前 `JSON.parse` 校验且必须为对象，失败不提交并保持弹窗 | 实测非法 `{invalid json` → 报「JSON 解析失败：Expected property name or '}' in JSON at position 1」，POST 次数 0；合法 JSON → POST body `"params":{"temperature":0.2,"max_tokens":1500}` |

### 二、P2 三项（全部完成）

| 项 | 实现位置 | 验证结果 |
| --- | --- | --- |
| P2-7 离线提示条 | 新增 `components/OfflineBanner.tsx`，挂在 Layout 的知识库列表页顶部；文案含「当前未配置模型，运行在离线演示模式」+ 具体缺哪个模型 + 「去配置模型」链接（仅 admin）/「不再提示」（写 `localStorage.wrk_offline_banner_dismissed`） | 实测提示条出现且文案正确；点「不再提示」后消失、localStorage 置 1；关于页有「重置离线提示」按钮可恢复 |
| P2-8 后端未就绪兜底 | 新增 `components/BackendGate.tsx`，包住整个 `<Routes>`；`initApiBase()` 拿不到 apiBase 直接判失败；否则 `probeBackend('/meta')` 6 秒超时。失败页展示连接地址/运行模式/数据目录 + 3 条排查建议 + 「重试连接」/「打开日志」（桌面版另有「重启后端」）+ 可展开日志（`getLogs` + 复制） | 用 CDP `Fetch.failRequest` 拦截 `/api/meta` 模拟断网 + 整页 reload → 兜底页正常出现，「重试连接」「查看日志」按钮均在；解除拦截后点重试 → 恢复进入系统 |
| P2-9 关于页 | 新增 `pages/AboutPage.tsx` + 路由 `/about` + 侧边栏「关于」。两块：版本信息（应用名/前端版本/后端版本/初始化/模型状态与 capabilities/当前登录）、运行环境（模式/后端地址/数据目录可点击打开/日志目录+日志内容/Electron）；第三块后端连通性（状态徽标 + 往返耗时 + 版本 + 重新检测） | 实测「后端运行正常 · 26 ms · v1.0.0」；数据目录在浏览器模式正确显示「浏览器模式不可用」 |

### 三、附带修复

- `index.html` 加了内联 SVG favicon（data URI），消掉所有页面的 `favicon.ico 404` console 报错。
- `api.ts` 新增 `probeBackend()`（不走 `request()`，避免 401 触发全局登出）与 `metaApi.health()`。
- `api.ts` 的 `chatApi.preview()` 归一化：后端 `routes` 是数组，前端组装成 `{kb_routes, stats}`，与 `ask` 一致。
- `types.ts` 补 `EvidenceItem.coverage`、`RouteStats` 扩展字段、`SearchPreviewRaw`、`MetaCapabilities`、`DesktopInfo`、`AuditEntry` 中文映射。

### 四、验收方式与结果

用 CDP（Chrome headless）驱动真实浏览器，后端跑在 **8001**（8000 被其他 teammate 占用）+ 独立数据目录，前端 Vite 5173 通过 `WRK_API_TARGET` 代理到 8001。

**10 个路由全量巡检：0 console error / 0 exception / 0 HTTP 4xx-5xx**

```
kbs / kbs1-docs / kbs1-wiki / kbs1-settings / chat / search / tasks / about / users / models
```

> 注：巡检前有一次 `favicon.ico 404`，加内联 favicon 后归零。

### 五、踩到的坑（下次注意）

1. **Vite dev server 会在 `index.html` 被原子写入时崩溃**：`EBUSY: resource busy or locked, watch '.index.html.<pid>.<uuid>.tmpdir/index.html.tmp'`。表现是页面 bodyLen 骤降到 91 字节（"无法访问此网站"）。**改 index.html 后必须确认 vite 还活着**，必要时重启。
2. **后端 8000 端口常被其他 teammate 占用**。用 `WRK_API_TARGET=http://127.0.0.1:8001` 起 vite 即可，不用改 `vite.config.ts`。
3. **改后端代码后必须重启 uvicorn**：我启动服务后 `retrieval.py` 被其他 teammate 改动，旧进程加载的是旧代码，`/api/search/preview` 直接 500（`NameError: stats_obj`）。重启后正常。
4. **CDP 里 `Network.setRequestInterception` 已废弃**，模拟网络失败要用 `Fetch.enable` + `Fetch.failRequest`。
5. **BackendGate 只在首次挂载时判定**。测兜底页必须整页 reload（`Page.reload`），在应用内路由跳转不会重新探活。

### 六、仍未完成（不在本次范围）

1. Wiki 版本历史仍看不到历史正文（后端 `/versions` 不返回 `content_md`），前端已有空态兜底。
2. 历史消息没有 `insufficient`/`usage` 字段，前端用「无证据 + 有 route」近似。
3. 模型 `params` 只做通用 JSON 编辑，没有按用途给出字段提示（如 embedding 的 `dimensions`）。
4. 检索调试页没有「保存本次实验」功能；论文复现建议直接复制上下文预览或截图。

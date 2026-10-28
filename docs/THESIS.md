# 论文素材：企业多知识库的文档整理与 Wiki 增强问答系统

本文是可直接改写进学位论文的章节素材：**结论 + 代码依据**，不含过程叙事。所有参数名、函数名、接口路径、默认值均取自当前仓库；凡未实测的数字一律标注「待实验」并给出测量方法，不填推测值。

- 系统名 `企业知识库文档整理与问答系统`，版本 `1.0.0`（`backend/app/main.py:23-24`）
- 代码位置约定：`文件:行`，路径相对仓库根
- 已有实测数据的来源见 §7 的取证表；未复跑的旧数据会显式标注出处

---

## 0. 一句话定位

面向**单个企业内部**的知识检索与整理系统：把散落在 PDF/Word/Excel/PPT/Markdown 里的制度与流程，加工成「原文分块索引」与「模型归纳的 Wiki 页面」两种可检索形态，在同一套问答链路上按库自动路由、双层融合召回、强制引用溯源，并在证据不足时输出结构化拒答。

**可声明的贡献边界**（写论文时必须守住，否则会被质疑）：

1. 不提出新的检索模型或新的融合算子；RRF、BM25 变体、余弦检索都是成熟方法。贡献在**工程化的取舍**与**可度量的成本口径**。
2. 不做多租户、分布式、在线学习；单机 SQLite 是刻意的部署约束，不是能力缺陷的掩盖（§2.1）。
3. Wiki 生成是**有损归纳**，不承诺事实保真，只承诺「证据可回挂 + 覆盖范围如实上报」。

---

## 1. 问题定义与设计取舍

企业文档问答有三个与开放域 QA 不同的约束，它们决定了系统形态：

| 约束 | 具体表现 | 系统应对 |
| --- | --- | --- |
| 数据不出企业 | 制度文件含薪酬、客户名单 | 单机部署；`HTTP_TRUST_ENV=0` 默认不继承系统代理（`backend/app/config.py:72-74`）；原文、分块、向量全落本机 |
| 零运维 | 没有专职运维，双击图标就要能用 | Electron 拉起 Python sidecar，动态端口，`WRK_READY` 握手（`backend/app/desktop_entry.py:59-70`） |
| 答案必须可追溯 | 用户要能核对到「第几条第几款」 | 回答带 `[编号]`，编号反查 `Chunk.locator` / `WikiEvidence`（`backend/app/services/answering.py:397-405`） |

**为什么不做微服务**：目标规模是几十到几百人、单库 1000 份文档量级。服务发现、独立数据库、容器编排在这里都是纯成本；`backend/app/db.py` 用 `journal_mode=WAL` + `busy_timeout=15000` 支撑「HTTP 线程 + 2 个 Worker 线程」的并发写（`WORKER_CONCURRENCY=2`，`backend/app/config.py:22`），足以替代外部队列。

**为什么索引有两种形态**：原文结构清晰、以查阅为主（条款、参数表）时，二次归纳只会引入失真；原文零散、同一流程散落在多个文件时，归纳成页才有价值。两种需求在同一企业内并存，因此用 `KnowledgeBase.mode` 表达，而不是全局二选一（`backend/app/models.py:29-31`）。

---

## 2. 系统设计

### 2.1 分层架构

```
Electron 33（外壳）  →  Python 3.13 + FastAPI（sidecar）  →  SQLite + 本地文件
   窗口/菜单/日志          路由·检索·生成·任务队列              19 张表 + 原文副本
```

| 层 | 关键实现 | 职责边界 |
| --- | --- | --- |
| 外壳 | `frontend/electron/main.cjs` | 只做进程生命周期、IPC、日志；不含业务逻辑 |
| 服务 | `backend/app/main.py`、`backend/app/routers/*`、`backend/app/services/*` | 全部业务；对渲染进程只暴露 `/api/*` |
| 存储 | `data/db/app.sqlite3`、`data/files/`、`data/secrets/` | 结构化数据 + 原文副本 + 加密密钥 |

**sidecar 契约**（可写进「系统实现」章的工程细节）：

- 端口：`WRK_PORT=0` → `bind((host, 0))` 由系统分配，解决多实例与端口冲突（`backend/app/desktop_entry.py:33-45`）。
- 就绪：轮询最多 600 × 50 ms，端口可连即打印 `WRK_READY {"host","port","version","app","pid"}`；主进程按行匹配该前缀并 `resolve(backendInfo)`（`frontend/electron/main.cjs:198-230`）。**不能只查 `server.started`**——它在 lifespan 完成后才置位，晚于端口 bind。
- 退出：`watch_stdin()` 阻塞读 stdin，父进程退出即 EOF → `server.should_exit = True`；主进程另有 `stdin.end()` + `SIGTERM` + 4 秒后 `SIGKILL` 的双保险（`backend/app/desktop_entry.py:80-90`）。Windows 上 Electron 被强杀时子进程收不到信号，stdin EOF 是唯一可靠路径。
- 失败可读：后端启动失败按 `EADDRINUSE / vcruntime / ENOENT / 超时 / 退出码` 翻译成「发生了什么 + 下一步做什么」（`frontend/electron/main.cjs:30-73`），不把退出码直接甩给用户。

### 2.2 数据模型

19 张表（`backend/app/models.py`），四组：

| 组 | 表 | 关键字段 |
| --- | --- | --- |
| 身份与授权 | `users` `kb_grants` `audit_logs` `app_settings` | `role∈{admin,employee}`；`level∈{read,manage}` |
| 知识库与文档 | `knowledge_bases` `documents` `document_versions` `chunks` | `Chunk(text, heading, locator, keywords, vector_blob, embed_signature, kind, meta)` |
| Wiki | `wiki_pages` `wiki_page_versions` `wiki_sections` `wiki_evidence` | `human_locked`、`current_version_id`、`heading_path` |
| 运行与成本 | `tasks` `index_versions` `cost_records` `providers` `model_configs` `conversations` `messages` | `Task(kind,status,stage,progress,dedupe_key)`；`CostRecord` 分通道计数 |

三处刻意的设计（论文里可作为「数据模型取舍」小节）：

1. **`Chunk.kb_id` 冗余**：单库检索是最高频操作，`_chunk_rows()` 直接 `where(Chunk.kb_id == kb_id)`，省一次 join；索引 `ix_chunk_kb_version` 建在 `(kb_id, doc_version_id)` 上，同时服务「按库取候选」与「按版本下线旧块」。
2. **`Message.evidence` / `route` 是快照不是引用**：回答当时命中哪些块、走 wiki 还是 chunk、是否 fallback、`usage` 多少，全部序列化进消息行（`backend/app/routers/chat.py:151-162`）。文档更新或索引重建后，历史会话仍能还原「当时看到了什么」。
3. **`IndexVersion` 目前只写不读**：三处 `db.add(IndexVersion(...))`，全项目无查询使用。论文中只能写「索引构建留痕」，不能写「索引版本管理与回滚」。

### 2.3 双形态索引与逐库路由

| 维度 | `basic` | `wiki` |
| --- | --- | --- |
| 检索单元 | `Chunk` | `WikiSection`（回退时用 `Chunk`） |
| 单元来源 | 原文 → `split_blocks()` | 页面正文 → `split_markdown()` |
| 组织结构 | 扁平，靠 `heading` 字符串关联 | 两层：`WikiPage` → `WikiSection`，`heading_path` 带页面名 |
| 生成成本 | 一次解析 + 一次向量化 | 主题归纳 + 每页生成 + 全部章节向量化 |
| 更新代价 | 单文档增量 | 全库重建 |

**路由判定 `plan_route()`**（`backend/app/services/retrieval.py:562-585`）解决「配置了 wiki ≠ Wiki 现在能用」：

```python
mode == "wiki":
    存在已发布且 current_version_id 非空的页面 → ("wiki", "Wiki 已就绪，检索 Wiki 章节", fallback=False)
    存在任意 Chunk                              → ("chunk", "Wiki 尚未生成完成，已回退到分块索引", fallback=True)
    否则                                        → ("none", "Wiki 未就绪且没有可用分块索引…", fallback=True)
mode == "basic":
    存在任意 Chunk → ("chunk", …, False)；否则 ("none", …, False)
```

两点必须写清：**逐库独立判定**（多库混合时 A 库走 wiki、B 库走 chunk、C 库不可用，`KbRoute` 逐条记录）；**回退不静默**（`fallback=True` 会在前端路由面板与拒答文案里显式列出）。另有一条二次回退：`used == "wiki"` 但 `search_wiki()` 返回空时再试 `search_chunks()`，命中则改用分块并置 `fallback=True`（`backend/app/services/retrieval.py:619-632`），覆盖「Wiki 已就绪但与本次查询无关」。

### 2.4 任务队列、幂等与失败语义

`Task` 表即队列，进程内 2 个 Worker 线程轮询。**抢占靠条件更新**：`update(Task).where(id==, status=="queued").values(status="running")`，`rowcount != 1` 即放弃，两个线程不会重复执行（`backend/app/services/worker.py` 的 `_claim()`）。`dedupe_key` 防同类任务堆积。

失败分三级，粒度必须区分：

| 级别 | 字段 | 语义 |
| --- | --- | --- |
| 任务 | `Task.status=failed` + `error` | 本次执行失败，可 `POST /api/tasks/{id}/retry` |
| 文档版本 | `DocumentVersion.status=failed` | 这个版本没进来 |
| 知识库 | `KnowledgeBase.status` | **取决于库里还有没有可用索引单元**：`remaining` 非空置 `partial`，为空才置 `failed` |

第三级最容易写错：旧版本分块在新版本失败时是**保留**的，检索仍能命中；无条件置 `failed` 会让界面宣称「库坏了」，而它完全可用。Wiki 生成失败单独处理——只置 `kb.status=partial` 并写 `wiki_error`，不推翻已建好的分块索引（Wiki 是增强，不是前置依赖）。

**幂等三则**：① 同一版本重跑先 `delete(Chunk).where(doc_version_id == version.id)`，不累积重复块；② **「新版本成功入库后才下线旧版本分块」**——写完新块并 `flush()` 之后才 `delete(... doc_version_id != version.id)`，顺序颠倒会导致新版本解析失败时用户同时失去新旧两份索引；③ Wiki 按 `slug` 复用页面并递增 `version_no`，不产生重复页。

---

## 3. 关键实现

### 3.1 文本分块：标题是硬边界

参数：`DEFAULT_CHUNK_SIZE = 700`、`DEFAULT_CHUNK_OVERLAP = 120`（`backend/app/config.py:33-34`）。

| 规则 | 行为 | 理由 |
| --- | --- | --- |
| 标题变化强制切分 | `kind=="heading"` 或 `block.heading` 与当前不同 → `flush(carry_overlap=False)` | 不切会把「考勤」与「请假」并进同一块，命中后无法判断归属，Wiki 主题也会串 |
| overlap 只在同标题内 | 仅「同标题段落聚合溢出」传 `True`；heading / table / image 边界、超长段前、函数末尾全部 `False` | 换标题带 overlap 会把上一节内容混进新章节 |
| 超长段落按句切分 | `len(text) > size * 1.6`（1120 字符）时用 `_SENT_SPLIT` 按 `。！？!?；;` 断句，累积到 `size` 即 emit，回退 **1 句** | 中文句长方差大，按字符回退容易把句子劈成两半 |
| 过短块丢弃 | `len(text) < 8` 不 emit | 避免标点、页码成为检索单元 |

`overlap` 参数的实现细节值得单独写一句（这是一个真实的接口陷阱）：`backend/app/services/chunking.py:164-166` 用 `if overlap is None` 而非 `overlap or ...`，因为 `overlap=0` 是合法输入（表示关闭重叠），用 `or` 会被当成假值替换成 120，调用方显式关掉重叠却毫无效果。

**准确表述**：同一标题内段落聚合溢出时保留 120 字符尾部重叠；超长段落按句切分时回退 1 句；标题/表格/图片边界处不重叠。

### 3.2 表格分块：字符预算是硬约束，行数是软目标

`split_table()`（`backend/app/services/chunking.py:76-154`）的三条策略与取舍：

| 策略 | 后果 |
| --- | --- |
| 逐行切分 | 一行是「工号: E0231；姓名: 王五；部门: 销售部…」，用户问「销售部客户经理薪酬范围」时命中行只给一个人的月薪；且大量重复列名让 700 个向量高度相似，检索无法区分；向量化批次也翻十几倍 |
| 整表一块 | 59,403 字符远超 embedding 输入窗口，**截断是静默的**——不报错、不返回标记，块尾数据没进索引且不可观测。碎块只是效果差，截断是数据丢失 |
| 行组 + 表头重复（现实现） | 每块自带表头与「表：X（共 N 行），本段为第 a-b 行」，可独立检索；`locator` 记行区间，证据能定位回原始行 |

装行算法：`budget = max(TABLE_CHUNK_BUDGET - header_overhead, 200)`，逐行累加，`cur_len + line_len > budget` 立即封组——**不让行数做主导**，因为超预算会被 embedding 静默截断，而表格单行长度方差极大（同为 9 列，短行 84 字符，长行可到几百）。小表（整表能放进预算或行数 ≤ 3）整块保留，`total <= 25` 时用 Markdown 表格渲染以保留可读性。

已有实测（`docs/DESIGN.md` 附录，`backend/scripts/inspect_chunks.py` 实跑）：

| 文件 | 规模 | 逐行 | 整表 | 行组 + 表头（现实现） |
| --- | --- | --- | --- | --- |
| `员工薪酬明细_120行.docx` | 120×9 | 120 块，均长 83.9 | 1 块 / 10,201 字符 | **7 块**，1055–1558 字符，均 17.1 行 |
| `员工薪酬明细_700行.docx` | 700×9 | 700 块，均长 83.8 | 1 块 / 59,403 字符 | **39 块**，1385–1560 字符，均 17.9 行 |
| `员工薪酬明细_200行.xlsx` | 200×9 + 6×4 | 206 块 | 1 块 / 16,979 字符 | **13 块**，193–1564 字符 |

39 块的行区间拼起来正好 1..700，连续无重叠无遗漏。**已知限制**：单行单元格 > 1600 字符时该行仍超预算——当前视「行」为原子单位，拆行会破坏行内字段对应关系，需产品决策。

### 3.3 Wiki 生成：Map-Reduce 分片归纳

这是本系统与「朴素 RAG」差异最大的一处，也是论文最值得展开的实现。

**问题**：单库要支撑 1000 份文档（约 3000–5000 块），一次请求装不下。早期实现只取**前 60 块**送进主题归纳，导致库尾内容永远进不了 Wiki，且对「Wiki vs 分块」的对比实验构成致命不公平（Wiki 只看得到 1/3 资料）。

**现实现**（`backend/app/services/wiki.py`）：

```
_plan_shards(chunks) → [(全局编号, chunk)] 的分片列表 + 单片摘录长度 excerpt
  ├─ _shard_chunks(chunks, excerpt):
  │    编号在切片之前用 enumerate(chunks, start=1) 固定
  │    单块成本 cost = min(len(text), excerpt) + 60
  │    累积超 WIKI_SHARD_CHAR_BUDGET 或片内块数达 TOPIC_SAMPLE_SIZE(60) 即换片
  ├─ 若片数 > WIKI_MAX_MAP_CALLS(40) 且 excerpt > WIKI_TOPIC_EXCERPT_MIN(120):
  │    ratio = limit / len(shards); nxt = max(floor, int(excerpt * ratio * 0.9))
  │    重切，直到片数达标或摘录触底
  └─ 触底仍超上限 → 如实多调用（不丢库尾内容）
for shard in shards: _map_topics(...)        # Map：每片独立归纳，失败按片降级
topics = _merge_topics(topics)               # Reduce：合并相近主题
若 len(topics) > WIKI_MAX_PAGES(60)：按证据数保留前 60 并写 notes
```

四条必须写进论文的约束：

1. **全库每一块都进入某一片**。这是与旧实现的本质区别，也是「Wiki 覆盖全库」这一实验前提的来源。
2. **片内使用全库全局编号**，绝不从 1 重编。历史上正是因为离线分支用全量编号、下游只在前 60 条里查找，80 块时 12 个主题只产出 1 页（`backend/app/services/wiki.py:94-99` 的注释记录了该缺陷）。
3. **降级是按片发生的**：某片模型调用失败只让该片退回离线聚类，其它片不受影响；但 `stats.mode` 一旦有一片降级就标记 `offline`，统计口径必须诚实（`backend/app/services/wiki.py:182-231`）。
4. **覆盖范围如实上报**：分片数 > 1 时 `WikiBuildResult.notes` 写「全库共 N 个分块，按字符预算分 S 片归纳后合并（单片摘录 E 字，未丢弃任何分块）」；页面数超限时写「合并后共 T 个主题，按证据数量保留前 60 个；其余内容仍可通过分块检索命中」。**不静默截断**是可复现性的前提。

**Reduce 的合并规则**（`_should_merge`，`backend/app/services/wiki.py:251-282`）从严到宽：

| 规则 | 条件 | 作用 |
| --- | --- | --- |
| 1 | 归一化标题（去空白/标点/大小写）完全相同 | 离线聚类跨片必然命中 |
| 2 | 短标题被长标题包含且短标题 ≥ 4 字 | 「住宿标准」并入「住宿标准与报销」 |
| 3 | 数字序列相同 + 短标题 ≥ 5 字 + (相似度 ≥ 0.72，或 ≥ 0.45 且证据重叠 ≥ 30%) | 防止把「第1节…」「第2节…」并成一页（相似度 0.9 但语义不同，实测会丢 11 个页面） |

合并保留首次出现的标题与顺序（贴近阅读顺序），证据编号取并集去重，上限 `MAX_TOPIC_CHUNK_IDS = 12`；`offline` 标记仅当两侧都离线才保留，因为离线摘要里带片段数，合并后必须按并集重算。

**规模参数**：`MAX_TOPICS = 12`（单次/单片最多主题数）、`CHUNKS_PER_TOPIC = 6`（每主题喂给页面生成的分块数）、`WIKI_MAX_PAGES = 60`。Map 调用次数 ≈ `ceil(总字符 / 9000)`，上限由 `WIKI_MAX_MAP_CALLS` 约束。

**人工校对保护**：`human_locked` 页面在重建时降为 `needs_review`，正文不动，`pages_skipped += 1` 并在 notes 写明。理由很实际——重建是常态操作（每次入库都会触发），没有锁定保护，任何一次上传都会冲掉校对成果。

### 3.4 检索：两层融合，两层公式不同

**这是论文里最容易写错的地方：库内与跨库用的是两套完全不同的融合逻辑，必须分两层写。**

#### 第一层：库内 —— 加权 RRF

`search_chunks()` / `search_wiki()` 各跑两个通道：`keyword_rank()` 产出 `kw_ids`，`vector_rank()` 产出 `vec_ids`，再由 `rrf_fuse([kw_ids, vec_ids], weights=channel_weights(...))` 融合（`backend/app/services/retrieval.py:269-289`）：

```
score(d) = Σ_c  w_c / (k + rank_c(d)),     k = RRF_K = 60
w = channel_weights(db, qvec_ok):
    qvec 缺失          → [1.0, 0.0]
    离线哈希向量        → [1.0, 0.45]
    真实 embedding     → [1.0, 1.0]
```

**为什么不比较原始分数**：关键词分是 BM25 变体的加权和，量纲随 `idf`、`tf`、文档长度浮动，可到几十分；余弦相似度是有界量。直接相加或加权平均没有意义——权重会随语料规模漂移，换个库就得重调。RRF 只用**名次**，天然消除量纲差异，代价是丢失「差多少」的信息。

关键词通道的真实行为（`keyword_rank`，`backend/app/services/retrieval.py:76-147`）：`idf = log(1 + (n_docs - df + 0.5)/(df + 0.5))`，`df` 与 `avg_len` 按当前候选语料实时计算；`k1 = 1.4`、`b = 0.72`；命中 `keywords` 字段 ×1.8；命中 `heading` +0.9；覆盖率加成 `×(1 + 0.18 × (hits-1))`；入选门槛 `hits >= 2` 或存在长度 ≥ 3 的查询词命中；门槛过严导致零结果时回退到「原始命中 + `log1p(tf)`」的宽松打分。

向量通道的词汇闸门（`vector_rank`，`backend/app/services/retrieval.py:150-198`）：`cutoff = max(min_sim, best × rel_ratio)`，`rel_ratio = 0.55`、`min_sim = 0.05`；`lexical_ids` 非空时只保留「语义命中且关键词也命中」的结果，交集为空则放行全部（当作同义改写）。用相对最高分的比例作主判据，因为不同模型的相似度分布差异很大，固定绝对阈值必然在某一类模型上失效。

> 论文写作提醒：`bm25_score()` 是死代码（全项目零调用），**不要引用它**作为实现依据。

#### 第二层：跨库 —— 覆盖率 × 名次衰减 × 均衡因子

`retrieve()` 拿到每库 top-N（`PER_KB_CANDIDATES = 8`）后**不使用 RRF**（`backend/app/services/retrieval.py:642-703`）：

```
coverage(d)  = |{t ∈ Q : t ∈ heading(d) ∪ text(d)}| / |Q|     # 0~1，绝对量
rank_term    = 1 / (1 + 0.45 × local_idx)                      # 库内名次衰减
balance      = 1.0                        if local_idx == 0
               1 / (1 + 0.10 × kb_idx)    otherwise            # 库间均衡
score(d)     = (0.25 + coverage(d)) × rank_term × balance
```

三个分量的设计理由：

- `0.25 + coverage` 是跨库**唯一可比**的量：它是 0~1 的绝对比例，与检索方式、库大小、通道权重无关。
- `rank_term` 只做粗排：`local_idx = 0/1/2` 时为 `1.0 / 0.690 / 0.526`，库内精细排序已由加权 RRF 决定。
- `balance` 只作用于库内第 2 名之后，保证各库首位候选平等竞争，避免大库占满名额。

排序后 `coverage > 0` 的候选在前、为 0 的接在后面（补足上下文但不占名额）；最终条数 `total = MAX_EVIDENCE(10) if top_k is None else max(top_k, 3)`——**`top_k` 有下限 3**，且同一参数被复用作「每库候选数」，这是已知的接口语义复用。

**必须记住的副作用**：`coverage` 与最终 `rank` **不是单调关系**。已有实测（`docs/DESIGN.md:222`）：查询「P0 工单首次响应时限」，rank 1 是 coverage 0.38 的正文段落，rank 2 是 coverage 0.62 的工单分级表格——同属一个库，`rank_term` 的 1.0 vs 0.690 压过了 `(0.25+coverage)` 的 0.63 vs 0.87。因此**「证据覆盖率」与「检索名次」必须作为两个层次分别统计**，不能用一个指标代替另一个。

`coverage` 已对外输出：`Evidence.to_dict()` 含 `coverage`，`/api/search/preview` 的 `evidence[]` 可直接读到；`stats` 里给出 `query_terms`（截前 16 个）、`candidate_total`、`final_total`、`rrf_k`、`kb_coverage`。

### 3.5 证据链与引用校验

```
Message.content        回答正文，结论后带 [编号]
  → Message.evidence[] 本轮证据快照（JSON）
    → Evidence.key     "chunk:{chunk_id}" | "wiki:{page_version_id}:{heading}"
      → Chunk.locator  "第 3 页" / "表格 1 第 685-700 行" / "表格 1 全表"
      → WikiEvidence   wiki 证据经此回挂原文：chunk_id + quote + locator
```

`_citation_check()`（`backend/app/services/answering.py:397-405`）用 `\[(\d{1,2})\]` 抽取回答中的编号，与证据列表长度比对，返回 `{cited, invalid, citation_ok}`。`invalid` 非空即「模型编了不存在的编号」——这是一个**可观测的幻觉信号**，可直接作为实验指标。

### 3.6 拒答：两级判定 + 固定文案结构

判定函数 `_refusal_verdict()`（`backend/app/services/answering.py:122-149`）**不看覆盖率硬阈值**，两级：

1. **开头 80 字内命中拒答语句 → 一律判拒答，不看引用**。因为模型被要求在回答最前面原样输出前置句；旧逻辑 `(not cited) and any(marker in text)` 只要模型在硬答后补个 `[1]`，检测就失效。
2. **没有任何有效引用 + 全文含拒答语气 → 判拒答**（覆盖模型没按格式开头的情况）。

拒答文案是固定三段结构（`backend/app/services/answering.py:6-20`）：

```
现有资料不足以回答这个问题。        ← REFUSAL_LEAD，所有拒答路径统一前置
<可选：缺什么 / 哪些库不可检索 / 哪些库发生了回退>
本次检索到这些资料：                ← 本次真实命中，heading/doc_name/page_title 去重
- 《知识库》文档 · 小节
你可以试试这样问：                  ← 来自库内真实标题，默认 5 条
1. 住宿标准是怎么规定的？
```

建议项是**结构化**的：`[{kb_name, kind, title, question?}]`，`kind = "resource" | "question"`，`title` 一定是库内真实文本；取不到任何真实标题时退化为「可以补充上传相关文档，或换一个更具体的问题。」，不生成通用话术（`backend/app/services/answering.py:195-372`）。

**已知取舍**（论文局限性里要写）：像「资料中没有提到加班餐补，但住宿标准是 600 元[1]」这种半答半拒的回答也会被判为拒答（拒答语句出现在开头）。这是刻意的保守方向——宁可多提示一次「证据不足」，也不放过用资料外知识硬答。因此**「无答案题正确率」不能只读 `insufficient` 字段，必须人工判定**。

### 3.7 成本核算：三通道 + 阶段归因

`CallStats`（`backend/app/services/llm.py:58-220`）是唯一的计数入口，字段语义（全部为累计值）：

| 字段 | 语义 |
| --- | --- |
| `calls` | 总调用 = `chat_calls + embed_calls + vision_calls`（含离线降级） |
| `chat_calls / embed_calls / vision_calls` | 分通道调用次数 |
| `live_calls / offline_calls` | 真实供应商调用 / 离线降级调用，二者之和 = `calls` |
| `prompt_tokens / completion_tokens` | 生成侧输入/输出 |
| `embed_tokens` | 向量侧 token，**不并入** `prompt_tokens` |
| `embed_texts` | 被向量化的文本条数（离线模式也记，衡量索引工作量） |
| `latency_ms` / `latency_chat_ms` / `latency_embed_ms` | 总/分通道耗时 |
| `mode` / `embed_mode` | **生成通道**模式 / **向量通道**模式，分开表达 |

**为什么必须分三通道**（这是本文最能站住脚的工程论点）：

1. 计费口径不同：生成按输入/输出分别计价，向量通常只按输入计价且单价常低一个数量级，混进同一个 `prompt_tokens` 就算不出真实成本。
2. 归因不同：一次问答只有 1 次生成调用，但索引阶段可能有成百上千次向量调用；合并计数会让「问答成本」被索引成本淹没。
3. 离线降级必须可识别：哈希向量不产生费用，但确实是一次向量化操作。所以 `calls` 照记，用 `live_calls` 区分——论文只报 `live_calls` 就是真实开销。
4. `mode` 只描述生成通道：若把「只有 embedding 走离线」也算 `mode=offline`，回答明明由真实模型生成，界面却说没调模型，这是对用户撒谎。

**流水与聚合**：`cost_records` 表按 `(kb_id, kind, source, source_id, doc_id)` 落流水（`backend/app/services/cost.py:71-97`）；`GET /api/kbs/{id}/cost` 聚合出 `totals / by_group{ingest,wiki,other} / by_stage / by_mode{live,offline} / records`（`backend/app/services/cost.py:166-244`）。`by_group` 正是论文要的两列对比：**索引成本（ingest）vs 整理成本（wiki）**。全零统计不写记录（避免「成本记录条数」失去意义）；无 `kb_id` 的调用（如连接测试）不进业务成本表。升级前的历史任务没有流水，从 `Task.result["usage"]` 按 `task_id` 去重回读，`legacy_records` 暴露该数量。

三处曾经漏记、现已修复的调用点（可作为「成本口径完整性」的论证）：检索侧查询向量化、Wiki 章节向量化、人工校对后的重建索引；失败任务已消耗的调用单独记 `failed_attempt` 阶段（`backend/app/services/cost.py:26-43`）。

### 3.8 模型协议适配与向量签名隔离

三种协议集中在 `backend/app/services/llm.py` 一层分派（`chat/embed/describe_image/test_model` 的调用方只关心「给我一段文本」）：

| `protocol` | 生成 | Embedding | 鉴权 |
| --- | --- | --- | --- |
| `openai` | `POST {base}/chat/completions` | `POST {base}/embeddings` | `Authorization: Bearer` |
| `openai-responses` | `POST {base}/responses`（`input` / `max_output_tokens` / `output[].content[].text`） | **无端点** | `Authorization: Bearer` |
| `anthropic` | `POST {base}/messages`（`system` 独立字段、`max_tokens` 必填、`content[].text`） | **无端点** | `x-api-key` + `anthropic-version` |

两条硬约束：

1. **不静默降级**。`responses` / `anthropic` 缺 Embedding 端点时直接抛 `ModelUnavailable`（`backend/app/services/llm.py:466-471`、`backend/app/services/llm.py:653-655`），而不是退回哈希向量——静默降级会让用户以为向量检索正常，实际排序质量已塌。
2. **向量签名硬隔离**。`kb.embed_signature` 形如 `openai:text-embedding-3-small:1536`；`search_chunks()` 只取签名一致的块，新旧模型的维度与语义空间不同，混用会让余弦相似度失去意义。切换必须全量重建：接口层 `if has_index and not confirm: 409`，确认后为每个有库建 `embedding_rebuild` 任务（`backend/app/routers/settings.py:267-321`）。签名不一致期间新文档**仍按库内旧签名写入**（`backend/app/services/ingest.py:74-76`），即「不允许在未重建时混用新模型向量」，`GET /api/settings/embedding/status` 的 `mismatch` 暴露该状态。

**离线降级**：无配置时 `active_embedding()` 返回 `local-hash-embedding:256`（`FALLBACK_EMBED_DIM = 256`），`hash_embed()` 是确定性哈希向量；LLM 侧退回抽取式。链路可跑，但**排序与回答质量不能作为实验数据**，且 `channel_weights()` 据此把向量通道压到 0.45——这一差异会直接改变融合结果，实验记录必须注明当前配置。

---

## 4. 实验方法（待实验）

本节只给**可执行的测量方案**，不填任何未跑出的数字。所有采集都走现成接口，不需要改代码即可完成 E1–E3、E5；E4 需要探针脚本复现另两种分块策略。

### 4.1 实验矩阵

| 编号 | 研究问题 | 自变量 | 控制变量 | 状态 |
| --- | --- | --- | --- | --- |
| E1 | Wiki 增强是否优于纯分块 | `KnowledgeBase.mode ∈ {basic, wiki}`，同一批文档 | 同一份分块、同一题集、同一向量来源 | 待实验 |
| E2 | 单库检索 vs 多库混合是否引入噪声 | `kb_ids` 单元素 vs 三元素 | 同一题集，题目按「答案所在库」标注 | 待实验 |
| E3 | 两层融合各分量的贡献 | 加权 RRF / 等权 RRF / 纯关键词 / 纯向量 | 同一语料与题集 | 待实验（后两组需改 `channel_weights()` 与 `lexical_ids`） |
| E4 | 表格分块策略 | 逐行 / 整表 / 行组+表头 | 同一份表格文件 | 待实验（仅第三种是现成实现） |
| E5 | 分块粒度与 overlap 的敏感性 | `WRK_CHUNK_SIZE ∈ {400,700,1000}` × `WRK_CHUNK_OVERLAP ∈ {0,120,240}` | 同一语料与题集 | 待实验 |

**E1 的公平性前提已变化**：旧版 Wiki 只覆盖前 60 块，扩料后直接对比会低估 Wiki。现实现下全库每块都进入某一片（§3.3），因此 E1 可以放开到 150–300 块；但仍需在结果里核对 `WikiBuildResult.notes` 的分片数与页面数上限提示，确认没有因 `WIKI_MAX_PAGES=60` 丢掉主题——若丢过，必须在论文中披露「Wiki 页面覆盖率」这一额外变量。

**E5 的实施方式**：改环境变量后需重建索引（`POST /api/kbs/{id}/rebuild`），不同 `WRK_CHUNK_SIZE` 的结果必须落在不同知识库或不同 `WRK_DATA_DIR`，否则分块表被覆盖。`WRK_CHUNK_OVERLAP=0` 是合法输入（`backend/app/services/chunking.py:164-166` 已修正），可真正关闭重叠。

### 4.2 指标定义

| 指标 | 定义 | 采集方式 |
| --- | --- | --- |
| Hit@k | 前 k 条证据中含「预期命中单元」的比例（k ∈ {1,3,10}） | `evidence[].rank` 与人工标注比对 |
| MRR | `mean(1/rank_first_hit)` | 同上 |
| 证据覆盖率 | `evidence[].coverage` 的均值 / 首位值 | `/api/search/preview` 直接返回 |
| 引用有效率 | `citations.cited` 非空且 `invalid` 为空的比例 | `ask` 响应的 `citations` |
| 引用精确率 | 人工判断「该编号是否真的支撑该句」 | 人工标注 |
| 拒答正确率 | 无答案题判 `insufficient=True`、有答案题判 `False` 的比例 | **必须人工判定**，不能只读字段（§3.6） |
| 整理成本 | `by_group.ingest` / `by_group.wiki` 的 `live_calls`、`total_tokens`、`latency_ms` | `GET /api/kbs/{id}/cost` |
| 问答成本 | 单次 `ask` 的 `usage.live_calls` / `total_tokens` / `latency_ms` | `ask` 响应 `usage` |
| 检索耗时 | `search/preview` 的端到端时间（不含生成） | 采集脚本计时 |

**必须分层报告的两组数**：`coverage` 与 `rank` 不单调（§3.4），「证据覆盖率」与「检索名次」要分开统计；`live_calls` 与 `offline_calls` 要分开统计，否则离线演示的零成本会污染费用结论。

### 4.3 采集与统计

现成工具 `backend/scripts/collect_metrics.py`：对一组知识库跑一组问题，逐条落 CSV，列为
`run_label, repeat, kb_ids, kb_modes, question, mode, evidence_count, top1_coverage, mean_coverage, source_types, answer_chars, citation_count, invalid_citations, insufficient, latency_ms, calls, chat_calls, embed_calls, live_calls, offline_calls, prompt_tokens, completion_tokens, embed_tokens, total_tokens, stats_mode, embed_mode, error`

```bash
# 只测检索（快，不消耗生成调用）
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe backend/scripts/collect_metrics.py \
  --base-url http://127.0.0.1:8000 --username admin --password '<密码>' \
  --input questions.json --out search.csv --mode search --repeat 3

# 完整问答（含生成成本）
… --mode ask --out ask.csv --label wiki-700
```

`--repeat 3` 用于观察波动（CSV 的 `repeat` 列区分），`--label` 给每轮打标签便于多轮合并。`questions.json` 支持 `{"kb_ids":[...], "questions":[...]}` 或纯数组（后者用 `--kb-ids` 传库）。

**样本量与显著性**：建议 60 题、四类各 15（单点事实 / 流程步骤 / 跨节归纳 / 无答案）。配对设计（同一题在两个条件下各跑一次）用 **Wilcoxon 符号秩检验**；比例类指标（Hit@1、拒答正确率）用 **McNemar 检验**；成本类（token、耗时）分布明显右偏，报告中位数与 IQR 而非均值。**待实验**：上述检验的实际 p 值。

### 4.4 必须声明的混淆因素

1. **向量来源必须统一**。混用离线哈希与真实 embedding 会让 `channel_weights()` 在 0.45 与 1.0 之间跳变，融合结果不可比。实验配置需三选一并写进论文：A 全离线（仅验证流程）、B 全在线（主结果）、C 生成在线 + 向量离线（隔离「生成质量」与「向量质量」）。
2. **模型随机性**。`ModelConfig.params` 覆盖调用默认值（`temperature`、`max_tokens`），实验应显式固定为 `{"temperature": 0.2, "max_tokens": 1500}`。
3. **`top_k` 的下限 3** 与「每库候选 8」是同一参数，跨库对比时要固定 `top_k`，否则单库组的候选池被无意扩大。
4. **语料规模**。`demo_data` 三库仅 25 个检索单元，任何一题答对都可能是偶然命中，统计上没有区分度。目标规模 150–300 单元（`docs/EXPERIMENT.md` §2.2 给出了扩充路径）。
5. **索引与查询同源**。索引阶段与查询阶段必须用同一个 embedding 模型（签名一致），否则向量通道实际失效而关键词通道仍在工作，结果会呈现「看起来还行」的假象。

### 4.5 待实验清单（逐项给测量方法）

| 待测项 | 方法 |
| --- | --- |
| 真实 embedding 下的检索耗时 | 固定语料，`--mode search --repeat 10`，记录 `latency_ms` 的 P50/P95；与离线路径分开报 |
| Wiki vs 分块的正确率差 | E1，配对 60 题，McNemar |
| Wiki 的整理成本倍数 | `by_group.wiki.live_calls / by_group.ingest.live_calls`（同一批文档） |
| 分片归纳对页面覆盖的影响 | 造 200/500/1000 块的库，读 `WikiBuildResult.notes` 与 `pages_created`，确认「未丢弃任何分块」成立 |
| 表格策略的可回答性差 | E4，题目含「末尾若干行」的事实题，验证整表策略的截断失效 |
| 多库噪声 | E2，统计 `final_total` 中 `coverage == 0` 且来自无关库的条数占比 |
| 拒答判定的假阳性率 | 人工标注 60 题，统计「有答案但被判 insufficient」的比例（预期非零，§3.6 的已知取舍） |

---

## 5. 可写的创新点（每条附可验证依据）

1. **两层异构融合的可解释设计**：库内用加权 RRF 消除量纲，跨库改用「查询词覆盖率 × 名次衰减 × 库间均衡」，并显式承认两者不可统一（`backend/app/services/retrieval.py:269`、`backend/app/services/retrieval.py:642-703`）。相比「全局一把 RRF」，跨库层用绝对覆盖率解决了「不同库的 RRF 分数不可比」的问题；相比「按库加权投票」，它不需要预先知道库的可信度。
2. **面向 embedding 静默截断的分块预算**：把「字符预算是硬约束、行数是软目标」写进表格分块（`backend/app/services/chunking.py:119-143`），并用表头重复保证每块可独立检索。这针对的是一类**不可观测的失败**（超长块被模型静默截断，块尾数据丢失且无信号），比「块偏碎」严重得多。
3. **全库覆盖的 Map-Reduce 主题归纳 + 降级诚实性**：分片、全局编号、超限先压摘录再如实多调用、覆盖范围写进 `notes`（`backend/app/services/wiki.py:119-308`、`backend/app/services/wiki.py:472-506`）。工程价值在于把「大库能不能整理」变成有界成本问题，同时不静默丢内容。
4. **可审计的三通道成本口径**：`chat/embed/vision` × `live/offline` 正交分解 + 阶段流水（`backend/app/services/cost.py`、`backend/app/services/llm.py:58-220`）。它让「索引成本 vs 整理成本」「真实费用 vs 离线演示」成为可直接查询的两个维度，而不是靠调用方自觉记账（历史上正是三处漏传 `stats` 导致系统性低估）。
5. **结构化拒答而非一句「不知道」**：固定前置句 + 本次真实命中资料 + 来自库内真实标题的可点击建议 + 逐库不可检索/回退原因（`backend/app/services/answering.py:6-20`、`backend/app/services/answering.py:181-192`）。把「答不出」变成可操作的下一步，同时保留「为什么判拒答」的 `refusal_reason` 便于审计。

---

## 6. 局限（论文必须自己写出来）

1. **单机与规模上限**。SQLite 单文件 + 进程内 Worker，单库 1000 份文档量级可用；再往上需要换存储与外部队列。检索的关键词通道按 term 计频需把全库文本取回 Python（约 0.59 s / 2.1 MB @ 5000 块，`backend/app/services/retrieval.py:11-19` 记录），用 FTS 预筛会改变 IDF 统计范围进而影响排序，属于改变语义而非加速，故未做。
2. **Wiki 是有损归纳**。主题数受 `MAX_TOPICS`、页面数受 `WIKI_MAX_PAGES` 约束；超限按证据数保留并写 notes，但「被略过的主题」确实不在 Wiki 里（仍可由分块检索命中）。Wiki 的事实保真度未做形式化保证，只保证证据可回挂。
3. **拒答判定是规则式**，存在已知假阳性（半答半拒被判拒答）与假阴性（模型换一种说法绕过标记词）。它不能替代人工判定。
4. **`IndexVersion` 只写不读**，不支持索引回滚；换 Embedding 只能全量重建，重建期间该库不可用（状态 `indexing`）。
5. **`wiki_ready` 有三个口径且当前不一致**：`backend/app/serializers.py` 的 `kb_out()` 按「已发布且 `current_version_id` 非空」、`backend/app/routers/kbs.py` 的 `_index_status()` 按「`WikiSection` 数 > 0」、`plan_route()` 用第一种。页面已发布但章节切不出来时前两者结论相反；检索以 `plan_route()` 为准，另两处只影响展示。
6. **视觉通道存在但弱**：图片只记元信息，未做内容理解（`backend/app/services/llm.py:687-697` 返回占位说明），`vision_calls` 统计在但实验里基本为 0。
7. **离线模式不可用于效果结论**。哈希向量与抽取式回答只保证链路可跑通，`channel_weights()` 会把向量通道压到 0.45，排序质量与在线配置不可比。

---

## 7. 已有实测数据的取证表

下表数字来自仓库内既有实跑记录，可直接引用；引用时请标注出处，不要当作本文新跑的实验结果。

| 数字 | 出处 |
| --- | --- |
| 700 行 → 39 块（1385–1560 字符，均 17.9 行）；120 行 → 7 块；200 行 xlsx → 13 块 | `docs/DESIGN.md` 附录；`backend/scripts/inspect_chunks.py` 实跑 |
| 整表 59,403 / 10,201 / 16,979 字符 | 同上（按 `_row_to_line()` 逐行拼接复算） |
| `demo_data` 三库 25 个检索单元 | `docs/EXPERIMENT.md` §2.1（`parse_document` + `split_blocks` 探针） |
| Wiki 8 块库产出 7 页 / 7 节 / 8 证据，`calls=0`（离线） | `docs/DESIGN.md:97`（探针灌临时 SQLite 跑 `build_wiki()`） |
| 检索耗时 min 6.0 / P50 6.8 / max 14.3 ms（离线哈希向量，9 条查询） | `docs/DESIGN.md` 附录；**接入真实 embedding 后必须重测** |
| 后端测试 157 passed, 1 skipped | `backend/tests/`（16 个文件、150 个 `def test_`，参数化后 157 项） |

---

## 8. 复现清单

| 要复现的结论 | 命令 / 接口 |
| --- | --- |
| 环境可用 | `.venv/Scripts/python.exe -m pytest backend/tests -q` |
| 演示三库 | `cd backend && PYTHONIOENCODING=utf-8 PYTHONPATH=. ../.venv/Scripts/python.exe -m scripts.seed_demo` |
| 表格分块产出 | `.venv/Scripts/python.exe backend/scripts/inspect_chunks.py` |
| 检索质量与成本逐条数据 | `backend/scripts/collect_metrics.py`（§4.3） |
| 单库成本分列 | `GET /api/kbs/{id}/cost` → `by_group.ingest` / `by_group.wiki` |
| 检索细节（覆盖率、查询词、候选数） | `POST /api/search/preview` → `evidence[].coverage`、`route.stats` |
| 拒答文案与原因 | `POST /api/conversations/{id}/ask` → `content`、`suggestions`、`refusal_reason`、`insufficient` |
| 向量来源与失配库 | `GET /api/settings/embedding/status` → `active`、`kbs[].mismatch` |
| 换模型强制重建 | `POST /api/settings/embedding/switch`（无 `confirm=true` 时 409） |

写作提醒：论文里凡引用「默认值」都要写成「本实现默认值」，因为这些参数全部可由环境变量覆盖（`backend/app/config.py`）；凡引用「实测」都要注明向量来源与是否离线，否则读者无法判断可比性。

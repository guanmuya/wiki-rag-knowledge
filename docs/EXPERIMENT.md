# 实验方案

本方案给出可复现、可执行的对比实验。所有参数、接口、字段名与当前代码一致；凡未能实测的一律标注「待实测」，不填推测值。

---

## 1. 实验环境与配置

### 1.1 硬件与系统

| 项 | 值 |
| --- | --- |
| 操作系统 | Windows（开发与验收环境） |
| Python | 3.13（项目 `.venv`） |
| 后端 | FastAPI + Uvicorn，单进程 |
| 数据库 | SQLite 单文件，`journal_mode=WAL`、`busy_timeout=15000` |
| 前端 | Electron 33 + React，仅在人工判定环节使用 |

CPU、内存、磁盘型号**待实测**（`systeminfo` 输出后补入）。离线哈希向量路径的耗时主要由 CPU 单核性能决定，接入真实 embedding 后主要受网络 RTT 影响，两者需分开记录。

### 1.2 模型配置

实验必须固定一种向量来源，否则不同库的检索质量不可比（`Chunk.embed_signature` 不一致的单元不参与向量检索）。三种可选配置：

| 配置 | 生成模型 | Embedding | 用途 |
| --- | --- | --- | --- |
| A 离线 | 无 | `local-hash-embedding`（256 维） | 验证链路、跑通全部实验流程 |
| B 在线 | 任一 OpenAI 兼容 `llm` | 任一 `embedding` 模型 | 论文主结果 |
| C 混合 | 在线 `llm` | 离线哈希 | 隔离「生成质量」与「向量质量」 |

配置 A 下 `channel_weights()` 返回 `[1.0, 0.45]`（向量通道降权），配置 B/C 下为 `[1.0, 1.0]`。**这一差异会直接改变融合结果，实验记录必须注明当前是哪种配置。**

模型参数从 `ModelConfig.params`（JSON）读取，`llm.chat()` 用 `params.get("temperature", temperature)` 与 `params.get("max_tokens", max_tokens)` 覆盖调用方默认值。实验应把 `params` 显式写成 `{"temperature": 0.2, "max_tokens": 1500}` 以固定随机性来源之一。

### 1.3 全部可调参数

均来自 `backend/app/config.py`，可用环境变量覆盖：

| 参数 | 值 | 环境变量 | 作用 |
| --- | --- | --- | --- |
| `DEFAULT_CHUNK_SIZE` | 700 | `WRK_CHUNK_SIZE` | 正文块目标字符数 |
| `DEFAULT_CHUNK_OVERLAP` | 120 | `WRK_CHUNK_OVERLAP` | 同标题内块尾重叠字符数 |
| `TABLE_CHUNK_BUDGET` | 1600 | `WRK_TABLE_CHUNK_BUDGET` | 表格块字符预算 |
| `RRF_K` | 60 | `WRK_RRF_K` | RRF 平滑常数 |
| `PER_KB_CANDIDATES` | 8 | `WRK_PER_KB_CANDIDATES` | 每库候选数 |
| `MAX_EVIDENCE` | 10 | `WRK_MAX_EVIDENCE` | 最终证据条数上限 |
| `CONTEXT_CHAR_BUDGET` | 9000 | `WRK_CONTEXT_CHAR_BUDGET` | 拼进提示词的上下文字符上限 |
| `WORKER_CONCURRENCY` | 2 | `WRK_WORKER_CONCURRENCY` | Worker 线程数 |
| `WORKER_POLL_SECONDS` | 1.0 | `WRK_WORKER_POLL_SECONDS` | 任务轮询间隔 |
| `MAX_UPLOAD_MB` | 50 | `WRK_MAX_UPLOAD_MB` | 单文件上限 |

`wiki.py` 内另有三个模块级常量，不受环境变量控制，改它们需要改代码：

| 常量 | 值 | 含义 |
| --- | --- | --- |
| `TOPIC_SAMPLE_SIZE` | 60 | 主题归纳的采样窗口（前 60 个分块） |
| `MAX_TOPICS` | 12 | 单次最多生成的主题/页面数 |
| `CHUNKS_PER_TOPIC` | 6 | 每个主题最多引用的分块数 |

`llm.FALLBACK_EMBED_DIM = 256`、`llm.FALLBACK_EMBED_NAME = "local-hash-embedding"`。

---

## 2. 数据集

### 2.1 现状：三库共 25 个检索单元

`demo_data/` 下三个 markdown 由 `backend/scripts/seed_demo.py` 灌成三个知识库：

| 知识库 | `mode` | 文件 | 检索单元 | 其中表格 | 字符数 | 单元长度区间 |
| --- | --- | --- | --- | --- | --- | --- |
| 人事制度库 | `wiki` | `员工手册.md` | 8 | 1 | 1543 | 55–330 |
| 客户支持库 | `basic` | `客户支持与运维手册.md` | 10 | 1 | 1463 | 70–239 |
| 产品与销售库 | `basic` | `产品与销售知识.md` | 7 | 1 | 1222 | 46–238 |
| **合计** | | | **25** | **3** | **4228** | |

`demo_data/fixtures/` 另有 6 个格式压力文件，当前**不参与** `seed_demo`：

| 文件 | 解析块 | 表格 | 分块 | 表格块 | 字符 min/avg/max |
| --- | --- | --- | --- | --- | --- |
| `员工薪酬明细_700行.docx` | 6 | 1 | 41 | 39 | 1385 / 1552 / 1560 |
| `员工薪酬明细_120行.docx` | 6 | 1 | 9 | 7 | 1055 / 1483 / 1558 |
| `员工薪酬明细_200行.xlsx` | 2 | 2 | 13 | 13 | 193 / 1446 / 1564 |
| `研发流程规范.docx` | 18 | 0 | 5 | 0 | 26–87 |
| `客户投诉处理指引.txt` | 7 | 0 | 3 | 0 | 39–50 |
| `差旅与采购管理办法.pdf` | 9 | 0 | 5 | 0 | 9–88 |

### 2.2 为什么必须扩料

25 个检索单元对 40–60 题的问题集来说太薄：平均每个单元要承载 2 道题，任何一题答对都可能是偶然命中，统计上没有区分度。目标规模 **150–300 个检索单元**。

扩充路径：

1. **把 fixtures 全部入库**。仅 `员工薪酬明细_700行.docx` 一次就提供 39 个表格块，`200行.xlsx` 再提供 13 个。这批数据的价值是「同质、可控、规模已知」，适合做表格分块实验。
2. **补 3–5 篇同领域长文档**。主题需与现有三库有交集但不重合，例如《采购管理制度》《信息安全事件响应流程》《客户成功运营手册》。每篇 3000–8000 字，预计贡献 15–40 个单元。
3. **控制每库规模均衡**。跨库实验（实验二）需要各库体量接近，否则「大库占满候选」会与「路由策略」混为一谈。建议每库 50–100 个单元。

### 2.3 必须注意的公平性陷阱：Wiki 采样窗口只有 60

`build_wiki()` 的主题归纳只使用 `chunks[:TOPIC_SAMPLE_SIZE]`，即**前 60 个分块**。`_chunks_for_kb()` 的排序是 `order_by(Chunk.doc_id, Chunk.ordinal)`，所以窗口按「文档 id 升序 + 文档内顺序」截取。

**后果**：库内分块超过 60 个时，第 61 个之后的内容**不会**参与 Wiki 生成。实测 80 个分块（前 60 块同一标题「公共章节」，第 61–80 块各自独立标题）的结果是：

```
WikiBuildResult(pages_created=1, pages_updated=0, pages_skipped=0,
                sections=1, evidence_links=6, mode='live',
                notes=['主题归纳只用了前 60 个分块（共 80 个），其余内容仍可通过分块检索命中'])
```

即 12 个主题只产出 1 页——12 个主题里只有「公共章节」落在采样窗内。

这个行为本身是正确的（会写 `notes` 明确告知，不会静默丢页），但它对实验一（原文分块 vs Wiki 增强）构成直接威胁：

- 若扩到 150–300 块，Wiki 只覆盖前 60 块，而分块基线覆盖全部。此时 Wiki 模式「答不出后 200 块的内容」不是方法缺陷，而是采样窗口造成的，直接对比会低估 Wiki。
- 构造题集时必须让**所有题目的答案都落在前 60 个分块内**，或把参与 Wiki 对比的库控制在 60 块以内。
- 论文中必须显式声明这一点，并把它作为「Wiki 增强的规模上限」写进局限性，而不是隐藏。

### 2.4 题集设计

建议 60 题，四类各 15 题：

| 类型 | 定义 | 判分要点 |
| --- | --- | --- |
| 单点事实 | 答案是一个具体数值、日期、名称 | 数字/名称完全一致 |
| 流程步骤 | 答案是一串有序步骤 | 步骤完整且顺序正确 |
| 跨节归纳 | 答案需合并同一文档两处以上内容 | 两处依据都命中 |
| 无答案 | 语料中确实没有答案 | 应明确说明不足，不得编造 |

每题需标注「预期命中单元」（`chunk_id` 或 Wiki 页 slug + 章节标题），这是算 Hit@k 的基准。

---

## 3. 四个对比实验

### 实验一：原文分块 vs Wiki 增强

**变量**：同一批资料、同一题集，`KnowledgeBase.mode` 分别为 `basic` 与 `wiki`。

**控制**：两库的分块完全一致（用同一份文档入库），仅 `mode` 不同；`build_wiki()` 完成后确认 `plan_route()` 返回 `used == "wiki"` 且 `fallback == False`，否则该库数据作废。

**测量**：正确性三档、证据支持率、Hit@k / MRR、整理成本、响应耗时。

**注意**：Wiki 库必须 ≤ 60 个分块（见 2.3），或题集全部落在前 60 块内。

### 实验二：单库 vs 多库混合路由

**变量**：`kb_ids` 传单元素 vs 传三个元素。

**控制**：同一题集，题目按「答案所在库」标注。单库组按题目归属分别只查对应库；多库组每次查全部三库。

**测量**：多库组额外记录 `route.stats.candidate_total`、`final_total`、`kb_coverage`，以及 `route.kb_routes[]` 里每库的 `used` / `fallback` / `keyword_hits` / `vector_hits` / `wiki_hits`。

**要观察的现象**：多库是否引入跨库噪声（无关库的证据挤占名额）。若最终 10 条证据里有大量 `coverage == 0` 的条目来自无关库，说明均衡因子 `balance` 不够。

### 实验三：消融——加权 RRF vs 等权 RRF vs 纯关键词 vs 纯向量

四组配置：

| 组 | 配置 | 实现方式 |
| --- | --- | --- |
| 加权 RRF（基线） | 离线向量时 `[1.0, 0.45]`，真实模型时 `[1.0, 1.0]` | 现成代码，`channel_weights()` |
| 等权 RRF | `[1.0, 1.0]` | 需改代码：`channel_weights()` 强制返回 `[1.0, 1.0]` |
| 纯关键词 | `[1.0, 0.0]` | 令 `qvec = None`，`channel_weights(db, False)` 自然返回 `[1.0, 0.0]` |
| 纯向量 | `[0.0, 1.0]` | 需改代码：`channel_weights()` 返回 `[0.0, 1.0]`，且 `vector_rank()` 的 `lexical_ids` 需置 `None` |

**后两组当前没有接口开关，必须改代码或打补丁。** 建议加一个仅测试用的环境变量（如 `WRK_CHANNEL_WEIGHTS`），在 `channel_weights()` 开头读取；不要在论文里声称「通过配置即可切换」。

**纯向量组的关键细节**：`search_chunks()` 调 `vector_rank(..., lexical_ids=set(kw_ids))`。若只把权重置 `[0,1]` 而不动 `lexical_ids`，向量通道仍受关键词结果约束，测出来的是「关键词过滤后的向量排序」，不是真正的纯向量。这一组必须同时把 `lexical_ids` 传 `None`。

### 实验四：表格分块——逐行 vs 整表 vs 行组 + 表头重复

三种策略在同一份表格上对比。当前只有「行组 + 表头重复」是现成实现，另两种需要在探针脚本里复现：

| 策略 | 复现方式 | 实测产出（700 行 × 9 列） |
| --- | --- | --- |
| 逐行 | 每行调一次 `_row_to_line(header, row)` 作为一个单元 | 700 块，均长 83.8 字符 |
| 整表 | 全部行拼成一个单元 | 1 块 / 59,403 字符 |
| 行组 + 表头重复 | `split_table()` 现实现 | **39 块**，1385–1560 字符，均 17.9 行 |

完整对照（三份文件，实测）：

| 文件 | 逐行 | 整表 | 行组 + 表头 |
| --- | --- | --- | --- |
| `员工薪酬明细_120行.docx`（120×9） | 120 块，均长 83.9 | 1 块 / 10,201 字符（正文预算 14.6×） | 7 块，1055–1558 字符，均 17.1 行 |
| `员工薪酬明细_700行.docx`（700×9） | 700 块，均长 83.8 | 1 块 / 59,403 字符（正文预算 84.9×） | 39 块，1385–1560 字符，均 17.9 行 |
| `员工薪酬明细_200行.xlsx`（200×9 + 6×4） | 206 块 | 1 块 / 16,979 字符（正文预算 24.3×） | 13 块（12 + 1），193–1564 字符 |

**测量要点**：

- **可回答性**：针对表格设计题目，如「工号 E0512 的月薪是多少」「销售部有多少人」「月薪最高的是谁」。逐行策略下这些题目需要跨多块拼装，命中率应显著下降。
- **索引规模**：块数、总字符数、向量化调用批次（`batch = 16`，700 块需 44 批，39 块需 3 批）。
- **证据可定位性**：命中后 `locator` 是否能定位到具体行。逐行策略的 `locator` 是单行，行组策略是行区间。
- **整表策略的截断效应**：用「表格末尾若干行」的题目验证——若模型只看得到前几十行，末尾题目必然答错。这是整表策略最直接的失效证据。

---

## 4. 评价指标

### 4.1 正确性三档

人工判定，两人独立标注，不一致处由第三人裁定。判定只看回答正文，不看检索结果。

| 档位 | 判定标准 | 计分 |
| --- | --- | --- |
| 正确 | 关键信息与原文一致；数字、名称、日期完全正确；流程步骤完整且顺序正确 | 1.0 |
| 部分正确 | 方向正确但缺少关键限定（如漏掉「需提前 3 个工作日」）、步骤有遗漏但无错序、范围表述过宽 | 0.5 |
| 错误 | 与原文矛盾、编造原文没有的数字或流程、答非所问 | 0 |

**无答案题的判定**：明确说明「资料不足」并指出缺少什么信息 → 正确；给出任何具体答案 → 错误。**不能依赖接口的 `insufficient` 字段**，理由见 4.6。

指标：`准确率 = (正确数 × 1.0 + 部分正确数 × 0.5) / 总题数`，同时报告「严格正确率」（只算正确档）。

### 4.2 证据支持率

对每条回答，标注者判断「回答中的每个事实性陈述是否能在返回的 `evidence[]` 中找到出处」。

```
证据支持率 = 有出处的陈述数 / 陈述总数
```

分母只算事实性陈述，不算过渡句、总结句、拒答声明。同时报告「引用有效率」：`citations.cited` 非空且 `citations.invalid` 为空的比例——`invalid` 非空说明模型编了不存在的编号。

### 4.3 Hit@k / MRR / 覆盖率

以题集标注的「预期命中单元」为基准，用 `POST /api/search/preview` 返回的 `evidence[]` 计算，不经过生成。

```
Hit@k  = 前 k 条证据里含预期单元的比例        k ∈ {1, 3, 5, 10}
MRR    = mean(1 / 首个命中位置)               未命中记 0
覆盖率 = 命中题数 / 总题数                    至少 1 条预期单元出现在证据里
```

预期单元的匹配用 `evidence_id`（`"chunk:{chunk_id}"` 或 `"wiki:{page_version_id}:{heading}"`）。Wiki 组的预期单元应标注为页面 slug + 章节标题，因为 Wiki 证据的 `chunk_id` 为 `None`。

**`top_k` 的下限陷阱**：`retrieve()` 里 `total = MAX_EVIDENCE if top_k is None else max(top_k, 3)`，传 `top_k=1` 实际返回 3 条。要算 Hit@1 必须从返回结果里自己截取第一条，不能依赖接口参数。

### 4.4 整理成本

系统已内置成本流水，不需要自己埋点。每次产生模型开销的整理操作写一条 `CostRecord`，由 `GET /api/kbs/{kb_id}/cost` 聚合。

**`CallStats` 字段**（全部是累计值）：

| 字段 | 含义 |
| --- | --- |
| `calls` | 总调用次数 = `chat_calls + embed_calls + vision_calls`（含离线降级） |
| `chat_calls` / `embed_calls` / `vision_calls` | 分通道调用次数 |
| `live_calls` / `offline_calls` | 真实供应商调用 / 离线降级调用，二者之和等于 `calls` |
| `prompt_tokens` / `completion_tokens` | 生成侧输入 / 输出 token |
| `embed_tokens` | 向量侧 token，**不**并入 `prompt_tokens` |
| `embed_texts` | 被向量化的文本条数（离线模式也有值，衡量索引工作量） |
| `latency_ms` / `latency_chat_ms` / `latency_embed_ms` | 总耗时与分通道耗时 |
| `mode` / `embed_mode` / `degraded` | 生成通道模式 / 向量通道模式 / 任一通道降级 |

**为什么生成与向量必须分列**：计费口径不同（生成按输入/输出分别计价，向量通常只按输入计价，单价常低一个数量级），归因也不同（一次问答只有 1 次生成调用，索引阶段可能有成百上千次向量调用）。合并计数会让「问答成本」被索引成本淹没。**论文只报 `live_calls` 就是真实开销**，`offline_calls` 是本地哈希，不产生费用但确实是一次操作。

**`/cost` 的返回结构**（实测）：

| 字段 | 形态 | 内容 |
| --- | --- | --- |
| `totals` | 对象 | 全库累计 |
| `by_group` | 对象 | `ingest` / `wiki` 两组，论文对比主指标 |
| `group_labels` | 对象 | 上述两组的中文标签 |
| `by_stage` | **列表** | 每项含 `stage`、`label` 与全部计数字段 |
| `by_mode` | 对象 | `live` / `offline` 分别累计，用来剔除离线演示的假开销 |
| `records` | 列表 | 最近 50 条明细 |
| `legacy_records` | 整数 | 从 `Task.result` 回读的历史条数 |

注意 `by_stage` 是列表而非字典，脚本取值时要按 `stage` 字段筛选。实测一次 `basic` 库入库的返回：

```
by_group: {'ingest': {'calls': 1, 'embed_calls': 1, 'offline_calls': 1,
                      'embed_texts': 8, 'record_count': 1}, 'wiki': {}}
by_stage[0]: {'stage': 'ingest_document', 'label': '文档入库', 'embed_texts': 8, ...}
```

**阶段拆分**。`ingest_version()` 产出 `result["usage_by_phase"]`，形如 `{"ingest_document": {...}, "rebuild_wiki": {...}}`，`worker` 用 `cost.record_phases()` 按阶段各写一条流水。这解决了「一次入库同时含分块向量化与 Wiki 生成、论文必须分列」的问题。人工校对后的章节重建不是任务，单独以 `kind="wiki_edit"` 记录（`source="wiki_edit"`）。

**历史数据兼容**。`CostRecord` 是较晚加入的表，老库里的任务只有 `Task.result["usage"]`。`kb_summary()` 用 `_legacy_rows()` 按 `task_id` 去重回读，保证升级前后数字连续。

**仍需人工计时的项**：Wiki 生成耗时可直接取 `Task.started_at` 到 `finished_at`；人工校对分钟/页只能用秒表，从打开页到 `PUT` 保存完成。

### 4.5 响应耗时 P50 / P95

两个口径分开测：

- **检索耗时**：`retrieve()` 端到端。当前离线哈希向量下实测 9 条查询 `min 6.0 / P50 6.8 / max 14.3 ms`，不含任何网络往返。接入真实 embedding 后必须重测。
- **端到端耗时**：`POST /api/conversations/{id}/ask` 的 `Message.latency_ms`（服务端记录），含检索 + 生成。

样本量至少 30 次/配置，报告 P50、P95、均值、标准差。冷启动首次调用单独记录（模型连接建立、SQLite 页缓存未热）。

### 4.6 两个不能直接用的字段

**`insufficient` 不能作为拒答判据**。判定逻辑是：

```python
insufficient = (not citations["cited"]) and any(m in text for m in REFUSAL_MARKERS)
```

是 `and`。只要模型带了任意合法引用编号，哪怕它同时在说「资料不足」，也不会判 `insufficient`；反之模型可能不带编号地说「根据资料……」被判成 `insufficient` 却是有效回答。**无答案题必须人工判定。**

**`coverage` 不能与 `rank` 混为一谈**。实测查询「P0 工单首次响应时限」：rank 1 的 `coverage` 是 0.38，rank 2 是 0.62。原因是两者同属一个库，库内名次衰减 `1/(1+0.45×local_idx)` 的 1.0 vs 0.690 压过了 `(0.25+coverage)` 的 0.63 vs 0.87。做检索指标时，「库内名次」与「跨库覆盖率」是两个层次，需分别统计。

---

## 5. 实验流程

### 5.1 环境准备

```bash
cd F:/GItHub_Code/Wiki-Rag-Knowledge

# 0) 确认代码静止、测试全绿（基线 111 passed, 1 skipped）
cd backend && PYTHONIOENCODING=utf-8 ../.venv/Scripts/python.exe -m pytest tests/ -q && cd ..

# 1) 隔离实验数据目录，绝不写 data/
export WRK_DATA_DIR=/tmp/wrk_exp/data
export WRK_DB_PATH=/tmp/wrk_exp/data/db/app.sqlite3
export WRK_SECRET_DIR=/tmp/wrk_exp/data/secrets
export WRK_WORKER_ENABLED=1
export PYTHONIOENCODING=utf-8
```

Windows PowerShell 等价写法：

```powershell
$env:WRK_DATA_DIR = "F:\wrk_exp\data"
$env:WRK_DB_PATH = "F:\wrk_exp\data\db\app.sqlite3"
$env:WRK_SECRET_DIR = "F:\wrk_exp\data\secrets"
$env:WRK_WORKER_ENABLED = "1"
```

### 5.2 灌数据

```bash
cd backend
PYTHONIOENCODING=utf-8 ../.venv/Scripts/python.exe -m scripts.seed_demo --reset
```

`seed_demo` 是幂等的，`--reset` 会先清空业务数据再导入。演示账号密码由 `scripts/seed_demo.py` 的 `DEMO_PASSWORD` 常量决定。

### 5.3 起服务

```bash
cd backend
PYTHONIOENCODING=utf-8 ../.venv/Scripts/python.exe -m app.desktop_entry
# stdout 会打印一行：WRK_READY {"host": "127.0.0.1", "port": <PORT>, ...}
```

后续命令里的 `$PORT` 取自该行的 `port` 字段。也可以直接 `uvicorn app.main:app --port 8000` 做实验，但那样就没有 `WRK_READY` 握手。

### 5.4 只跑检索（不生成回答）

`POST /api/search/preview` 是实验主力接口：只走 `retrieval.retrieve()` + `build_context()`，不调生成模型，因此**零生成成本、可高频重复**。

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:$PORT/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<DEMO_PASSWORD>"}' | jq -r .token)

curl -s -X POST http://127.0.0.1:$PORT/api/search/preview \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"kb_ids":[1,2,3],"query":"P0 工单首次响应时限"}' | jq .
```

返回 `{evidence[], routes[], stats, denied[], context_preview, usage}`。`evidence[]` 每条含 `evidence_id`、`source_type`、`score`、`rank`、`coverage`、`doc_name`、`version_no`、`locator`、`heading`、`channels`、`evidence`（Wiki 证据的原文回挂）。`stats` 含 `query_terms`、`embedding_ok`、`embedding_error`、`per_kb_limit`、`candidate_total`、`final_total`、`rrf_k`、`kb_coverage`。

`coverage` 已包含在 `evidence[]` 里，可直接用于复算。`usage` 是该次检索的成本（含查询向量化）。

### 5.5 跑完整问答

```bash
curl -s -X POST http://127.0.0.1:$PORT/api/conversations/1/ask \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"question":"P0 工单首次响应时限是多久？","kb_ids":[1,2,3]}' | jq .
```

返回 `{content, evidence[], route, usage, insufficient, citations, message, denied}`。`usage` 同时含生成与查询向量化两部分成本（`chat_calls` 与 `embed_calls` 分开记）。

### 5.6 采集整理成本

```bash
curl -s "http://127.0.0.1:$PORT/api/kbs/1/cost" -H "Authorization: Bearer $TOKEN" | jq .
```

`read` 权限即可读，不必持管理员凭据——这样实验脚本可以用普通员工账号跑问答。关注 `by_group.ingest` 与 `by_group.wiki` 两列的差值，那就是 Wiki 增强的净开销。注意 `by_group` 只有 `ingest` 与 `wiki` 两个键（`GROUP_OF_STAGE` 里定义的 `other` 组在无对应阶段时不出现）。

### 5.7 分块实测（表格实验）

```bash
cd F:/GItHub_Code/Wiki-Rag-Knowledge
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe backend/scripts/inspect_chunks.py
```

该脚本打印每个 fixture 的解析块数、表格数、分块数、字符 min/avg/max、行区间明细，有超长块时退出码 1。实验四的「行组 + 表头重复」数据直接取自它的输出。

### 5.8 批量跑题集

建议写一个脚本，把题集 JSON 逐条送 `search/preview`（算 Hit@k）与 `ask`（算正确性），结果落 CSV：

```
run_id, experiment, group, question_id, question, kb_ids,
hit_1, hit_3, hit_5, hit_10, mrr, top1_evidence_id, coverage_top1,
latency_ms, candidate_total, final_total, embedding_ok, rrf_k,
usage_calls, usage_chat_calls, usage_embed_calls, usage_live_calls,
usage_prompt_tokens, usage_completion_tokens, usage_embed_tokens,
insufficient_field, citations_invalid, answer_text,
label_correctness, label_evidence_support, reviewer
```

`label_*` 与 `reviewer` 由人工回填，其余字段脚本自动采集。

---

## 6. 预期结论与讨论

以下均为**待验证的预期**，不是实测结果。每组都写明「什么结果支持什么结论」与「反直觉结果如何解释」。

### 实验一

预期 Wiki 组在「跨节归纳」题上正确率更高、证据支持率更高，在「单点事实」题上与分块组持平或略低（二次归纳可能丢失精度）。

- **若 Wiki 全面优于分块**：说明二次整理确实降低了信息碎片度，但需检查是否因为 Wiki 章节更短、更容易被 top-k 覆盖——用「同等证据条数下的正确率」复核。
- **若 Wiki 在事实题上明显变差**：合理，说明归纳过程引入了精度损失。结论应是「Wiki 适合归纳类问题，不适合精确查数」，而不是「Wiki 无用」。
- **若 Wiki 组几乎无命中**：先查 `plan_route()` 的 `fallback` 是否为 `True`、`build_wiki()` 的 `notes` 是否提到采样窗口，再看库内分块数是否超过 60。

### 实验二

预期多库组在「答案分散在多个库」的题目上有优势，在单库可答题上因候选噪声略差。

- **若多库组 Hit@1 下降但 Hit@10 上升**：正常，说明跨库融合把正确证据挤到了后面。此时应报告 `coverage` 分布，判断是否是均衡因子不足。
- **若多库组全面下降**：检查 `kb_coverage` 是否被某个大库占满。若是，需调 `balance` 的 0.10 系数，但要说明这是超参调整，不是方法改进。

### 实验三

预期加权 RRF 优于等权 RRF（离线向量时差距应明显，真实模型时应缩小），纯关键词在实体类查询上强、纯向量在同义改写上强。

- **若等权 RRF 反而更好**：说明当前 0.45 的权重偏低，离线向量的排序质量比预期好。这是有价值的负结果，应如实报告并给出权重扫描曲线。
- **若纯向量组极差**：先确认 `lexical_ids` 是否已置 `None`。若未置，测的是「关键词门控后的向量」，不是纯向量。
- **若纯关键词优于全部融合方案**：在中文小语料 + 离线向量下完全可能。这说明该场景下向量通道没有贡献，应讨论「离线哈希向量是否值得接入」。

### 实验四

预期行组 + 表头重复在可回答性与索引规模上都优于另两者，逐行在需要跨行聚合的题目上显著失效，整表在表格末尾题目上显著失效。

- **若整表策略表现不差**：先确认测试题目是否集中在前几十行。整表策略的失效点是块尾截断，题目必须覆盖末尾行才能暴露。
- **若逐行策略在单点查数上最好**：合理，单行块与单点事实高度对齐。结论应是「逐行适合精确查数，行组适合范围/聚合类问题」，并讨论能否按查询类型动态选择。

---

## 7. 威胁与局限

**样本量**。`demo_data` 三库共 25 个检索单元，扩充到 150–300 后仍属小规模。60 题题集下，正确率差 5 个百分点只对应 3 道题，落在标注噪声范围内。所有对比都应报告置信区间或至少报告逐题明细，不能只给一个百分数。

**单一领域**。现有语料集中在企业人事、客户支持、产品销售三类。结论对其他领域（法律、医疗、工程图纸）的可迁移性未验证。跨领域效果差异主要来自分词与术语分布，本系统的关键词通道用中文二元组 + 英文词（`extract_keywords()`），对专业术语密集的领域可能退化。

**离线向量的局限**。`hash_embed()` 是基于 token 哈希的确定性向量，不含语义信息，只保证链路可跑通。实测查询「量子计算机的退相干时间是多少」（语料中完全无关）仍返回 7 条证据，首位 `coverage = 0.08`。这说明：

- 覆盖率门槛只用于排序让位（`coverage > 0` 的排前面），**不是过滤**，不能作为拒答判据；
- 离线向量下的检索指标不能外推到真实 embedding 场景。

论文主结果必须用配置 B（真实 embedding），配置 A 的结果只能作为「链路可运行」的证据。

**人工判定的主观性**。「部分正确」这一档的边界天然模糊——漏掉一个限定条件算部分正确还是错误，不同标注者会有分歧。缓解方式：两人独立标注 + 计算 Cohen's κ，κ < 0.6 时重新对齐判定标准再标。三档标准必须在标注开始前写死，中途不得修改。

**Wiki 采样窗口的规模天花板**。`TOPIC_SAMPLE_SIZE = 60` 意味着 Wiki 增强在当前实现下只覆盖前 60 个分块。扩料到 150–300 块后，Wiki 组与分块组的对比不再公平。这既是实验设计的约束，也是系统本身的局限——论文应把它写成「当前实现的规模上限」，并说明超限时的降级行为（`notes` 提示 + 其余内容仍可被分块检索命中）。

**单机环境的外部效度**。所有耗时数据来自单机、单进程、SQLite。多人并发场景下的锁竞争（`busy_timeout=15000`）、Worker 队列积压、以及 Electron 与 sidecar 的进程开销都未测量。P95 延迟在生产并发下会显著高于实验值。

**`top_k` 的语义混用**。`retrieve(top_k=N)` 里 N 同时是「每库候选数」和「最终条数」，且最终条数被 `max(top_k, 3)` 兜底。做 Hit@k 实验时若直接用 `top_k=k`，k=1 和 k=2 两组实际返回相同条数，会得出「k 不影响效果」的错误结论。

**成本口径的剩余盲区**。`CostRecord` 已覆盖任务、Wiki 编辑、失败任务（`kind="failed_attempt"`），但连接测试（`test_model()`）不写记录——它不属于任何库的整理代价。若实验里用连接测试探活，这部分开销不会出现在 `/cost` 里，需单独记录。

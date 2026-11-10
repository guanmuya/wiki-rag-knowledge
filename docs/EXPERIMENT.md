# 实验方案

本方案给出可复现、可执行的对比实验。所有参数、接口、字段名与当前代码一致；凡未能实测的一律标注「待实验」，不填推测值。

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

CPU、内存、磁盘型号已由本轮实测补入 `docs/results/environment.json`（见 §8.1）；CPU 具体型号在本机为虚拟机、系统把型号报告为 `Not Available`，按约定如实留白、不填推测值。离线哈希向量路径的耗时主要由 CPU 单核性能决定，接入真实 embedding 后主要受网络 RTT 影响，两者需分开记录。

### 1.2 模型配置

实验必须固定一种向量来源，否则不同库的检索质量不可比（`Chunk.embed_signature` 不一致的单元不参与向量检索）。三种可选配置：

| 配置 | 生成模型 | Embedding | 用途 |
| --- | --- | --- | --- |
| A 离线 | 无 | `local-hash-embedding`（256 维） | 验证链路、跑通全部实验流程 |
| B 在线 | 任一 OpenAI 兼容 `llm` | 任一 `embedding` 模型 | 论文主结果 |
| C 混合 | 在线 `llm` | 离线哈希 | 隔离「生成质量」与「向量质量」 |

配置 A 下 `channel_weights()` 返回 `[1.0, 0.45]`（向量通道降权），配置 B/C 下为 `[1.0, 1.0]`。**这一差异会直接改变融合结果，实验记录必须注明当前是哪种配置。**

**协议维度必须一并固定**。供应商支持三种协议（`Provider.protocol`）：

| 协议 | 生成端点 | 能否做 Embedding |
| --- | --- | --- |
| `openai` | `POST {base}/chat/completions` | 能（`POST {base}/embeddings`） |
| `openai-responses` | `POST {base}/responses` | 不能，`embed()` 直接抛 `ModelUnavailable` |
| `anthropic` | `POST {base}/messages` | 不能，同上 |

因此「生成用 anthropic / 向量用 OpenAI 兼容供应商」是允许的组合（两个 `ModelConfig` 各绑一个供应商），而「向量用 responses/anthropic」会在第一次索引时显式报错，不会静默降级成哈希向量。**换 Embedding 模型必须全量重建**：`POST /api/settings/embedding/switch` 在 `has_index and not confirm` 时返回 409，确认后为每个有分块的知识库建 `embedding_rebuild` 任务。实验中途换向量模型等于换掉整个语料的向量空间，之前的结果不可比，只能整组重跑。

模型参数从 `ModelConfig.params`（JSON）读取，`llm.chat()` 用 `params.get("temperature", temperature)` 与 `params.get("max_tokens", max_tokens)` 覆盖调用方默认值。实验应把 `params` 显式写成 `{"temperature": 0.2, "max_tokens": 1500}` 以固定随机性来源之一。注意 Wiki 生成内部写死了自己的温度与上限（主题归纳 `temperature=0.1, max_tokens=1200`，页面生成 `temperature=0.2, max_tokens=1400`），`params` 里的值会覆盖它们——**做 Wiki 实验时不要给 `params` 设 `temperature`**，否则两次实验的归纳风格不可比。

### 1.3 全部可调参数

环境变量部分来自 `backend/app/config.py`，可用环境变量覆盖：

| 参数 | 值 | 环境变量 | 作用 |
| --- | --- | --- | --- |
| `DEFAULT_CHUNK_SIZE` | 700 | `WRK_CHUNK_SIZE` | 正文块目标字符数 |
| `DEFAULT_CHUNK_OVERLAP` | 120 | `WRK_CHUNK_OVERLAP` | 同标题内块尾重叠字符数（`0` 合法，表示关闭） |
| `TABLE_CHUNK_BUDGET` | 1600 | `WRK_TABLE_CHUNK_BUDGET` | 表格块字符预算 |
| `RRF_K` | 60 | `WRK_RRF_K` | RRF 平滑常数 |
| `PER_KB_CANDIDATES` | 8 | `WRK_PER_KB_CANDIDATES` | 每库候选数 |
| `MAX_EVIDENCE` | 10 | `WRK_MAX_EVIDENCE` | 最终证据条数上限 |
| `CONTEXT_CHAR_BUDGET` | 9000 | `WRK_CONTEXT_CHAR_BUDGET` | 拼进提示词的上下文字符上限 |
| `WORKER_CONCURRENCY` | 2 | `WRK_WORKER_CONCURRENCY` | Worker 线程数 |
| `WORKER_POLL_SECONDS` | 1.0 | `WRK_WORKER_POLL_SECONDS` | 任务轮询间隔 |
| `MAX_UPLOAD_MB` | 50 | `WRK_MAX_UPLOAD_MB` | 单文件上限 |
| `WIKI_SHARD_CHAR_BUDGET` | 9000（默认等于 `CONTEXT_CHAR_BUDGET`） | `WRK_WIKI_SHARD_CHAR_BUDGET` | 单片 Map 调用的资料字符预算 |
| `WIKI_TOPIC_EXCERPT_CHARS` | 900 | `WRK_WIKI_TOPIC_EXCERPT_CHARS` | 单片内单个分块的摘录长度 |
| `WIKI_TOPIC_EXCERPT_MIN` | 120 | `WRK_WIKI_TOPIC_EXCERPT_MIN` | 摘录压缩下限 |
| `WIKI_MAX_MAP_CALLS` | 40 | `WRK_WIKI_MAX_MAP_CALLS` | Map 调用数上限（0 = 不限制） |
| `WIKI_MAX_PAGES` | 60 | `WRK_WIKI_MAX_PAGES` | 合并后页面数上限 |

`wiki.py` 内另有模块级常量，不受环境变量控制，改它们需要改代码：

| 常量 | 值 | 含义 |
| --- | --- | --- |
| `TOPIC_SAMPLE_SIZE` | 60 | **单片块数上限**（不是全库截断点；名字为兼容外部脚本保留） |
| `MAX_TOPICS` | 12 | **单片**最多归纳的主题数（不是全库上限） |
| `CHUNKS_PER_TOPIC` | 6 | 每个主题生成页面时最多引用的分块数 |
| `MAX_TOPIC_CHUNK_IDS` | 12 | 合并后单个主题最多记住的证据编号数 |
| `MERGE_TITLE_SIM` | 0.72 | 标题归一化相似度直接合并阈值 |
| `MERGE_TITLE_SIM_WITH_EVIDENCE` | 0.45 | 有证据重叠时放宽后的合并阈值 |
| `MERGE_EVIDENCE_OVERLAP` | 0.30 | 放宽阈值所需的证据重叠比例 |

`llm.FALLBACK_EMBED_DIM = 256`、`llm.FALLBACK_EMBED_NAME = "local-hash-embedding"`。

拒答侧还有两个不在 `config.py` 的条数开关：`WRK_SUGGESTION_LIMIT`（建议问题条数，默认 5）、`WRK_SUGGESTION_RESOURCE_LIMIT`（「本次检索到这些资料」最多几条，默认 6），均在 `answering.py` 本地读取。

**overlap 是全局单值，且 0 真的生效**。`chunking.split_blocks()` 用 `if overlap is None: overlap = config.DEFAULT_CHUNK_OVERLAP`，随后 `overlap = max(int(overlap), 0)`；生产调用点 `ingest_version()` 不传 `overlap`，因此 `WRK_CHUNK_OVERLAP=0` 会让入库路径完全关闭块尾重叠。这直接决定第 3 节实验五的 overlap 消融可执行——不需要改代码，改环境变量并重建库即可。注意两处细节：overlap 只在「同一标题内段落聚合溢出」时生效；超长段落按句切分时用 `acc = acc[-1:] if overlap else []`，`overlap=0` 同时关掉这句回退。

---

## 2. 数据集

### 2.1 现状：两个 seed 脚本，25 块与 55 块

`demo_data/` 下有 10 份 markdown，两个脚本按不同范围灌库（都用 `backend/scripts/` 下的同名模块）：

| 脚本 | 知识库 | `mode` | 文件数 | 检索单元 |
| --- | --- | --- | --- | --- |
| `seed_demo` | 人事制度库 | `wiki` | 1（`员工手册.md`） | 8 |
| `seed_demo` | 客户支持库 | `basic` | 1（`客户支持与运维手册.md`） | 10 |
| `seed_demo` | 产品与销售库 | `basic` | 1（`产品与销售知识.md`） | 7 |
| `seed_demo` 合计 | | | 3 | **25** |
| `seed_expanded` | 人事制度库 | `wiki` | 4 | 19 |
| `seed_expanded` | 客户支持库 | `basic` | 3 | 20 |
| `seed_expanded` | 产品与销售库 | `basic` | 3 | 16 |
| `seed_expanded` 合计 | | | 10 | **55** |

数字由 `parser.parse_document` + `chunking.split_blocks` 实算（不是按标题数估）。逐文件单元数：员工手册 8、培训与职业发展制度 4、薪酬与福利说明 3、考勤与假期补充规定 4、客户支持与运维手册 10、故障案例库 6、客户沟通规范 4、产品与销售知识 7、产品功能详解 5、招投标与合同要点 4。

`demo_data/fixtures/` 另有 6 个格式压力文件，当前**不参与**任一 seed 脚本：

| 文件 | 解析块 | 表格 | 分块 | 表格块 | 字符 min/avg/max |
| --- | --- | --- | --- | --- | --- |
| `员工薪酬明细_700行.docx` | 6 | 1 | 41 | 39 | 1385 / 1552 / 1560 |
| `员工薪酬明细_120行.docx` | 6 | 1 | 9 | 7 | 1055 / 1483 / 1558 |
| `员工薪酬明细_200行.xlsx` | 2 | 2 | 13 | 13 | 193 / 1446 / 1564 |
| `研发流程规范.docx` | 18 | 0 | 5 | 0 | 26–87 |
| `客户投诉处理指引.txt` | 7 | 0 | 3 | 0 | 39–50 |
| `差旅与采购管理办法.pdf` | 9 | 0 | 5 | 0 | 9–88 |

### 2.2 为什么必须扩料

55 个检索单元对 44 题的问题集来说仍然偏薄：平均每个单元承载 0.8 道题，任何一题答对都可能是偶然命中。目标规模 **150–300 个检索单元**。

扩充路径（按性价比排序）：

1. **直接用 `seed_expanded` 起步**。它已经把 `demo_data` 全部 10 份 markdown 入库，得到 55 块、三库均衡（19/20/16），比 `seed_demo` 的 25 块更适合做对比实验。注意它的 `MAX_TOTAL_CHUNKS = 60` 硬断言与 `TOPIC_SAMPLE_SIZE` 警告是「全库只取前 60 块」时代的遗留（见 2.3），扩料前必须先放宽，否则脚本会拒绝导入。
2. **把 fixtures 全部入库**。`员工薪酬明细_700行.docx` 一次提供 39 个表格块，`200行.xlsx` 再提供 13 个。这批数据的价值是「同质、可控、规模已知」，适合做表格分块实验；建议不纳入 Wiki 对比组——表格块归纳成页面的收益低，还会挤占 `MAX_TOPICS` 的主题名额。
3. **补 3–5 篇同领域长文档**。主题需与现有三库有交集但不重合，例如《采购管理制度》《信息安全事件响应流程》《客户成功运营手册》。每篇 3000–8000 字，预计贡献 15–40 个单元。
4. **控制每库规模均衡**。跨库实验（实验二）需要各库体量接近，否则「大库占满候选」会与「路由策略」混为一谈。建议每库 50–100 个单元。

### 2.3 覆盖范围：全库分片归纳，用 notes 字段做实验记录

Wiki 生成已改为 Map-Reduce 分片归纳，**全库每一个分块都进入某一分片**，不存在「只覆盖前 60 块」。切片逻辑在 `wiki._shard_chunks()` / `_plan_shards()`：

- 分块编号在切片前用 `enumerate(chunks, start=1)` 固定为全库 1-based 序号，片内不重编号；
- 单片大小由 `WIKI_SHARD_CHAR_BUDGET`（默认 9000）反推，单块按 `min(len(text), excerpt) + 60` 计费，另受 `TOPIC_SAMPLE_SIZE = 60` 的单片块数上限约束；
- 片数超过 `WIKI_MAX_MAP_CALLS`（默认 40）时先压缩单块摘录（900 → 下限 120，每轮按 `limit/片数 × 0.9` 递减）；压到 120 字仍超上限就**如实多调用**，不丢块。

**实验记录字段**：`build_wiki()` 在片数 > 1 时写一条覆盖说明到 `WikiBuildResult.notes`，最终落到任务结果里：

```json
{"wiki": {"created": 12, "updated": 0, "skipped": 0, "sections": 37,
          "evidence_links": 48,
          "notes": ["全库共 320 个分块，按字符预算分 36 片归纳后合并（单片摘录 900 字，未丢弃任何分块）"]}}
```

读法（这是本节的核心，实验脚本必须解析下面几段）：

| notes 片段 | 记录的字段 | 用途 |
| --- | --- | --- |
| `全库共 {N} 个分块` | `wiki_total_chunks` | 与库内实际分块数对账，必须相等；不等说明构建时库已变 |
| `按字符预算分 {M} 片归纳后合并` | `wiki_shards` | Map 调用次数（= M，某片降级不改变次数）；实验的自变量之一 |
| `单片摘录 {X} 字` | `wiki_excerpt` | 摘录被压缩到多少；X < 900 说明触发了压缩，归纳质量可能下降 |
| `合并后共 {K} 个主题，按证据数量保留前 60 个` | `wiki_topics_merged` / `wiki_pages_capped` | 页面数被 `WIKI_MAX_PAGES` 截断时必须记录，否则 Wiki 组天然少覆盖 |

**notes 为空说明什么**：`WikiBuildResult.notes` 的四类内容（分片覆盖、主题超限、主题定位失败、人工锁定）一条都没有，即 `len(shards) == 1`、主题数未超 60、没有跳过、没有锁定页。因此 notes 为空可安全推断「单片归纳、未截断、未跳过」。若要拿到确切的片数与摘录长度，跑一段只读探针（不写库、不调模型）：

```bash
cd backend
PYTHONIOENCODING=utf-8 ../.venv/Scripts/python.exe - <<'PY'
from app.db import SessionLocal
from app import models
from app.services import wiki
db = SessionLocal()
kb = db.get(models.KnowledgeBase, 1)          # 换成目标库 id
chunks = wiki._chunks_for_kb(db, kb.id)
shards, excerpt = wiki._plan_shards(chunks)
print({"chunks": len(chunks), "shards": len(shards), "excerpt": excerpt,
       "shard_sizes": [len(s) for s in shards]})
PY
```

**对实验设计的影响**：

- 不再需要把 Wiki 对比库限制在 60 块以内，也不需要「题目答案必须落在前 60 块」。相反，**题集应刻意覆盖库尾分块**（最后一个分块所在的章节至少 2 题），用来证明库尾内容确实进了 Wiki。
- 分片数 M 与摘录 X 是必须记录的协变量：同一份语料在 `WIKI_SHARD_CHAR_BUDGET` 调小后 M 变大、单片上下文变短，归纳结果会变。跨组对比必须 M、X 一致，或至少在论文里报告。
- `WIKI_MAX_PAGES = 60` 触发时，被略过的主题只剩分块检索这一条路。实验一若 Wiki 组在长尾主题上变差，先查这条 note 再下结论。

### 2.4 题集设计

`demo_data/questions.json` 已有 44 题，四类各 11 题，可直接作为第一版题集：

| 类型 | `type` 值 | 题数 | 定义 | 判分要点 |
| --- | --- | --- | --- | --- |
| 单点事实 | `fact` | 11 | 答案是一个具体数值、日期、名称 | 数字/名称完全一致 |
| 跨库对比 | `cross` | 11 | 答案需合并两个库的内容 | 两库依据都命中，且不混淆 |
| 多跳归纳 | `multi_hop` | 11 | 答案需合并同一库内两处以上内容 | 两处依据都命中 |
| 无答案 | `no_answer` | 11 | 语料中确实没有答案 | 应明确说明不足，不得编造 |

每题带 `id`、`question`、`type`、`expected_kb`（预期命中的知识库名列表）、`answer_keywords`（判分关键词）、`note`（出题依据）。`expected_kb` 直接支持实验二的单库/多库分组；`answer_keywords` 只能做初筛，最终仍要人工判定（见 4.1）。

扩到 60 题时建议四类各 15 题，补充题目沿用同一结构，并额外标注「预期命中单元」（`chunk_id` 或 Wiki 页 slug + 章节标题），这是算 Hit@k 的基准。新增题目必须覆盖：库尾分块、表格行组边界、Wiki 合并后的页面（同一主题跨两份文档）。

---

## 3. 五个对比实验

### 实验一：原文分块 vs Wiki 增强

**变量**：同一批资料、同一题集，`KnowledgeBase.mode` 分别为 `basic` 与 `wiki`。

**控制**：

- 两库的分块完全一致（用同一份文档入库），仅 `mode` 不同；`build_wiki()` 完成后确认 `plan_route()` 返回 `used == "wiki"` 且 `fallback == False`，否则该库数据作废。
- 固定 `WIKI_SHARD_CHAR_BUDGET` / `WIKI_TOPIC_EXCERPT_CHARS` / `WIKI_MAX_MAP_CALLS` / `WIKI_MAX_PAGES`，并在结果表里记录 `wiki_shards`、`wiki_excerpt`、`wiki_topics_merged`、`wiki_pages_capped`。
- 题集必须包含库尾题（2.3），否则「全库覆盖」这条改动没有被测到。

**测量**：正确性三档、证据支持率、Hit@k / MRR、整理成本（`by_group.ingest` 是索引成本、`by_group.wiki` 是 Wiki 净开销，两者分列报告）、响应耗时、Wiki 页面数/章节数。

**要观察的现象**：Wiki 组在 `multi_hop` 与 `cross` 上是否提升；`fact` 题是否因二次归纳丢精度；库尾题是否与库首题表现一致（不一致说明分片仍有盲区）。

### 实验二：单库 vs 多库混合路由

**变量**：`kb_ids` 传单元素 vs 传三个元素。

**控制**：同一题集，题目按 `expected_kb` 标注。单库组按题目归属分别只查对应库（`cross` 题按 `expected_kb[0]` 只查一个库，作为「信息不全」的对照组）；多库组每次查全部三库。

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
| 行组 + 表头重复 | `split_table()` 现实现 | **39 块**，1385–1560 字符，首块 1-18 行、末块 685-700 行 |

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

### 实验五：overlap 消融（0 / 60 / 120）

**变量**：`WRK_CHUNK_OVERLAP ∈ {0, 60, 120}`（120 为默认值），可选加 240 探上界。

**可执行性**：不需要改代码。`chunking.split_blocks()` 的默认值取 `config.DEFAULT_CHUNK_OVERLAP`，`overlap=0` 是合法输入（`if overlap is None` 才取默认值），生产调用点 `ingest_version()` 不传该参数，因此改环境变量即可全局生效（`config.py` 在进程启动时读取环境变量，改完必须重启后端）。每次切换必须**重建库**（分块边界变了，旧向量与旧题集标注都会错位）：

```bash
export WRK_CHUNK_OVERLAP=0          # 或 60 / 120
export WRK_DATA_DIR=/tmp/wrk_overlap/o0/data
export WRK_DB_PATH=/tmp/wrk_overlap/o0/data/db/app.sqlite3
export WRK_SECRET_DIR="$WRK_DATA_DIR/secrets"
cd backend && PYTHONIOENCODING=utf-8 ../.venv/Scripts/python.exe -m scripts.seed_expanded --reset
```

**控制**：同一语料、同一题集、同一 `mode`（建议 `basic`，避免 Wiki 归纳把分块差异抹平）、同一向量来源。三组的 `WRK_DATA_DIR` 必须彼此隔离。

**测量**：

| 字段 | 说明 |
| --- | --- |
| `overlap` | 本组取值（0/60/120） |
| `chunk_count` / `total_chars` / `avg_len` | 分块规模，`inspect_chunks.py` 或探针直接读 |
| `Hit@1/3/5/10`、`MRR` | 用 `POST /api/search/preview` 算，见 4.3 |
| `seam_question_hit` | **接缝题命中率**：专门构造 8–10 道「答案原句被切在块边界」的题（把关键数字/条件放在长段落中部），overlap 的作用只在这类题上体现 |
| `label_correctness` | 人工判定，见 4.1 |

**判定标准**：44 题规模下，Hit@k 差 1 题 ≈ 2.3 个百分点。若 0 与 120 的整体 Hit@5 差异不超过 1 题、且接缝题命中率差异明显，结论应写成「overlap 只对接缝类问题有效，对整体指标影响在噪声内」；若接缝题也无差异，说明当前 700 字符块长下重叠不是瓶颈，可把 `WRK_CHUNK_OVERLAP` 降到 60 以省索引体积（这是可写进论文的工程结论，但必须给出实测数字，不能推测）。

**注意**：`overlap=0` 同时关闭超长段落分支的「回退 1 句」（`acc = acc[-1:] if overlap else []`），因此两组差异不只来自 120 字符尾部重叠，也来自句级回退。写结论时必须点明这一点，否则会把两种机制混为一谈。

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

**`top_k` 的两个口径**：`retrieve()` 里 `total = MAX_EVIDENCE if top_k is None else max(top_k, 3)`（`retrieval.py:665`），同时 `per_kb = top_k or PER_KB_CANDIDATES`（`retrieval.py:596`）——**同一个 `top_k` 也是每库候选数**。所以传 `top_k=1` 不是「必回 3 条」：最终名额被抬到 3，但每库只出 1 条候选，单库时最终只返回 1 条；要凑满 3 条得 `top_k >= 3` 或有多库各出 1 条。`/api/search/preview` 的请求体没有 `top_k` 字段，固定按 `MAX_EVIDENCE = 10` 返回。要算 Hit@1 必须从返回结果里自己截取第一条，不能依赖接口参数。

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
| `notes` | 降级说明（同一句话只记一次） |

**为什么生成与向量必须分列**：计费口径不同（生成按输入/输出分别计价，向量通常只按输入计价，单价常低一个数量级），归因也不同（一次问答只有 1 次生成调用，索引阶段可能有成百上千次向量调用）。合并计数会让「问答成本」被索引成本淹没。**论文只报 `live_calls` 就是真实开销**，`offline_calls` 是本地哈希，不产生费用但确实是一次操作。

**三处必须传 `stats` 的路径（此前漏记，已修正）**，实验必须确认它们确实有值，否则成本数字偏低数倍：

| 路径 | 记录方式 | 验证字段 |
| --- | --- | --- |
| 检索查询向量化 | `retrieve(stats=...)` → `llm.embed(..., stats=cost)` | `ask` 响应 `usage.embed_calls >= 1` |
| Wiki 章节向量化 | `_index_sections(..., stats=stats)` | Wiki 任务 `usage_by_phase.rebuild_wiki.embed_calls > 0` |
| 人工校对重建索引 | `routers/wiki.py` 自建 `CallStats` + `cost.record(kind="wiki_edit")` | `PUT` 响应 `usage.embed_calls >= 1`，且 `by_stage` 出现 `wiki_edit` |

**`/cost` 的返回结构**（字段契约）：

| 字段 | 形态 | 内容 |
| --- | --- | --- |
| `kb_id` / `kb_name` / `mode` / `mode_label` | 标量 | 库标识与「Wiki 增强 / 基础分块」 |
| `totals` | 对象 | 全库累计（含 `total_tokens`、`record_count`） |
| `by_group` | 对象 | `ingest` / `wiki` 恒在（无流水时各项为 0），`other` 仅在存在 `failed_attempt` 流水时出现；论文对比主指标 |
| `group_labels` | 对象 | 上述三组的中文标签 |
| `by_stage` | **列表** | 每项含 `stage`、`label` 与全部计数字段，按 `calls` 降序 |
| `by_mode` | 对象 | `live` / `offline` 分别累计，用来剔除离线演示的假开销 |
| `records` | 列表 | 最近 50 条明细（倒序） |
| `legacy_records` | 整数 | 从 `Task.result` 回读的历史条数 |
| `my_level` | 字符串 | 调用者在该库的权限级别 |

注意 `by_stage` 是列表而非字典，脚本取值时要按 `stage` 字段筛选。结构示例（离线环境一次 `basic` 库入库）：

```
by_group: {'ingest': {'calls': 1, 'embed_calls': 1, 'offline_calls': 1,
                      'embed_texts': 8, 'record_count': 1},
           'wiki':   {'calls': 0, 'embed_calls': 0, 'record_count': 0, ...}}
by_stage[0]: {'stage': 'ingest_document', 'label': '文档入库', 'embed_texts': 8, ...}
```

**阶段拆分**。`ingest_version()` 产出 `result["usage_by_phase"]`，形如 `{"ingest_document": {...}, "rebuild_wiki": {...}}`，`worker` 用 `cost.record_phases()` 按阶段各写一条流水。这解决了「一次入库同时含分块向量化与 Wiki 生成、论文必须分列」的问题。人工校对后的章节重建不是任务，单独以 `kind="wiki_edit"` 记录（`source="wiki_edit"`）。

**失败任务也要计入**。任务失败前已经发生的调用是真金白银：`worker.run_once()` 在 `stats.calls or stats.embed_texts` 为真时写一条 `kind="failed_attempt"` 流水（归入 `other` 组），并把 usage 写回 `Task.result`。全零的失败（解析阶段就挂）不写记录——这是刻意的，避免「成本记录条数」失去意义。做失败率实验时，把 `failed_attempt` 的 `calls` 单独列一列，否则重试成本会凭空消失。

**历史数据兼容**。`CostRecord` 是较晚加入的表，老库里的任务只有 `Task.result["usage"]`。`kb_summary()` 用 `_legacy_rows()` 按 `task_id` 去重回读（已有流水的任务不再读 result），保证升级前后数字连续；坏任务（result 为空或 JSON 截断）直接跳过，不会把查询打成 500。升级老库做实验时，先看 `legacy_records` 是否为 0，非 0 说明统计混了两个来源。

**仍需人工计时的项**：Wiki 生成耗时可直接取 `Task.started_at` 到 `finished_at`；人工校对分钟/页只能用秒表，从打开页到 `PUT` 保存完成。

### 4.5 响应耗时 P50 / P95

两个口径分开测：

- **检索耗时**：`retrieve()` 端到端。当前离线哈希向量下实测 9 条查询 `min 6.0 / P50 6.8 / max 14.3 ms`，不含任何网络往返。接入真实 embedding 后必须重测（本轮未复测）。
- **端到端耗时**：`POST /api/conversations/{id}/ask` 的 `Message.latency_ms`（服务端记录），含检索 + 生成。

样本量至少 30 次/配置，报告 P50、P95、均值、标准差。冷启动首次调用单独记录（模型连接建立、SQLite 页缓存未热）。

### 4.6 三个不能直接用的字段

**`insufficient` 不能作为拒答判据**。判定逻辑已重写为 `_refusal_verdict()`：

```python
body = 去掉开头空白/引用符/列表符/加粗符后的全文
lead = body[:80]
for marker in REFUSAL_MARKERS:
    if marker in lead:                       # 不看引用
        return f"回答开头即声明资料不足（命中「{marker}」）"
if not citations["cited"]:
    for marker in REFUSAL_MARKERS:
        if marker in body:
            return f"没有有效引用且含拒答语气（命中「{marker}」）"
return ""
```

两个后果必须记住：① 模型带着 `[1]` 硬答、只要开头出现「资料中没有…」也会被判拒答（刻意的保守取舍）；② 半答半拒（「资料中没有提到 X，但 Y 是 Z[1]」）同样判拒答。因此**无答案题必须人工判定**，`insufficient` 只能用来交叉核对，不能当标签。拒答响应里额外有 `refusal_reason`（命中哪条语句）与结构化 `suggestions`（`kind=resource|question`），排查误判时先看 `refusal_reason`。这两个字段只在当次响应里返回，`Message.route` 只持久化 `insufficient` 与 `usage`。

**`coverage` 不能与 `rank` 混为一谈**。实测查询「P0 工单首次响应时限」：rank 1 的 `coverage` 是 0.38，rank 2 是 0.62。原因是两者同属一个库，库内名次衰减 `1/(1+0.45×local_idx)` 的 1.0 vs 0.690 压过了 `(0.25+coverage)` 的 0.63 vs 0.87。做检索指标时，「库内名次」与「跨库覆盖率」是两个层次，需分别统计。

**`citations.cited` 非空不等于有证据**。`_citation_check()` 只校验编号是否落在证据条数范围内，不校验编号对应的证据是否真的支撑该句。`invalid` 为空只能说明「没有编造编号」，不能说明「引用正确」——证据支持率必须人工标注。

---

## 5. 实验流程

### 5.1 环境准备

```bash
cd F:/GItHub_Code/Wiki-Rag-Knowledge

# 0) 确认代码静止、测试全绿（基线 157 passed, 1 skipped，16 个测试文件）
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

测试基线里与本方案直接相关的四组：`test_wiki_scale.py`（4 条，锁切片覆盖/压缩/千块级）、`test_wiki_sampling.py`（4 条，锁 80 块时不丢页）、`test_cost_metrics.py`（成本三通道与 `/cost` 结构）、`test_refusal.py`（6 条，锁拒答判定与建议块）。

### 5.2 灌数据

```bash
cd backend
# 三库 25 块（最小可用集）
PYTHONIOENCODING=utf-8 ../.venv/Scripts/python.exe -m scripts.seed_demo --reset
# 或：三库 55 块（推荐，实验一/二用它起步）
PYTHONIOENCODING=utf-8 ../.venv/Scripts/python.exe -m scripts.seed_expanded --reset
```

两个脚本都是幂等的，`--reset` 会先清空业务数据再导入。演示账号密码由脚本里的 `DEMO_PASSWORD` 常量决定。

**`seed_expanded` 的两个过期断言必须先处理**：`MIN_TOTAL_CHUNKS = 55` / `MAX_TOTAL_CHUNKS = 60`，以及 `report()` 里 `total > TOPIC_SAMPLE_SIZE` 就打印「Wiki 只会覆盖前 60 块，对比实验不再公平」并返回失败。这套判断是「全库只取前 60 块」时代的产物，与现在的分片归纳不符。扩料超过 60 块前，要么在脚本里放宽（改代码，属于实现变更），要么只把它当提示、忽略退出码。**论文里不能沿用「Wiki 覆盖前 60 块」这个说法**。

### 5.3 起服务

```bash
cd backend
PYTHONIOENCODING=utf-8 ../.venv/Scripts/python.exe -m app.desktop_entry
# stdout 会打印一行：WRK_READY {"host": "127.0.0.1", "port": <PORT>, ...}
```

后续命令里的 `$PORT` 取自该行的 `port` 字段。也可以直接 `uvicorn app.main:app --port 8000` 做实验，但那样就没有 `WRK_READY` 握手。

### 5.4 只跑检索（不生成回答）

`POST /api/search/preview` 是实验主力接口：只走 `retrieval.retrieve()` + `build_context()`，不调生成模型，因此**零生成成本、可高频重复**；查询向量化仍会产生一次 embed 调用，已计入返回的 `usage`。

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:$PORT/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<DEMO_PASSWORD>"}' | jq -r .token)

curl -s -X POST http://127.0.0.1:$PORT/api/search/preview \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"kb_ids":[1,2,3],"query":"P0 工单首次响应时限"}' | jq .
```

返回 `{evidence[], routes[], stats, denied[], context_preview, usage}`。`evidence[]` 每条含 `evidence_id`、`source_type`、`score`、`rank`、`coverage`、`doc_name`、`version_no`、`locator`、`heading`、`channels`、`evidence`（Wiki 证据的原文回挂）。`stats` 含 `query_terms`、`embedding_ok`、`embedding_error`、`per_kb_limit`、`candidate_total`、`final_total`、`rrf_k`、`kb_coverage`。

`coverage` 已包含在 `evidence[]` 里，可直接用于复算。`usage` 是该次检索的成本（含查询向量化，`embed_calls >= 1`、`embed_texts >= 1`）。

### 5.5 跑完整问答

```bash
curl -s -X POST http://127.0.0.1:$PORT/api/conversations/1/ask \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"question":"P0 工单首次响应时限是多久？","kb_ids":[1,2,3]}' | jq .
```

返回 `{content, evidence[], route, usage, insufficient, citations, suggestions, refusal_reason, message, denied}`。`usage` 同时含生成与查询向量化两部分成本（`chat_calls` 与 `embed_calls` 分开记）。拒答时 `suggestions` 是结构化数组（`kind=resource` 的条目来自本次命中的真实标题，`kind=question` 的条目可直接填入输入框）。

### 5.6 采集整理成本

```bash
curl -s "http://127.0.0.1:$PORT/api/kbs/1/cost" -H "Authorization: Bearer $TOKEN" | jq .
```

`read` 权限即可读，不必持管理员凭据——这样实验脚本可以用普通员工账号跑问答。`by_group.wiki` 就是 Wiki 增强的净开销，`by_group.ingest` 是索引成本，两者分列报告，不要相减。`by_group` 的键固定为 `ingest` / `wiki` / `other` 三个：前两个即使没有流水也会出现（值为全零），`other` 只在存在 `failed_attempt` 流水时才出现（`GROUP_OF_STAGE` 里 `failed_attempt → other`）。

### 5.7 分块实测（表格与 overlap 实验）

```bash
cd F:/GItHub_Code/Wiki-Rag-Knowledge
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe backend/scripts/inspect_chunks.py
```

该脚本打印每个 fixture 的解析块数、表格数、分块数、字符 min/avg/max、行区间明细，有超长块时退出码 1。本轮实测输出：700 行 docx → 41 块（39 表格 + 2 文本）、1385/1552/1560 字符、首块 1-18 行、末块 685-700 行；120 行 docx → 9 块（7 表格）、1055/1483/1558；200 行 xlsx → 13 块（12 + 1）、193/1446/1564；文本上限 910 字符（`700 × 1.3`），表格上限 1600。实验四的「行组 + 表头重复」数据直接取自它的输出；实验五换 `WRK_CHUNK_OVERLAP` 后重跑同一脚本即可对比块数。

### 5.8 批量跑题集

建议写一个脚本，把题集 JSON 逐条送 `search/preview`（算 Hit@k）与 `ask`（算正确性），结果落 CSV：

```
run_id, experiment, group, question_id, question, kb_ids, mode,
wiki_shards, wiki_excerpt, wiki_topics_merged, wiki_pages_capped,
hit_1, hit_3, hit_5, hit_10, mrr, top1_evidence_id, coverage_top1,
latency_ms, candidate_total, final_total, embedding_ok, rrf_k,
usage_calls, usage_chat_calls, usage_embed_calls, usage_vision_calls,
usage_live_calls, usage_offline_calls, usage_prompt_tokens,
usage_completion_tokens, usage_embed_tokens, usage_embed_texts,
usage_mode, usage_embed_mode, usage_degraded,
insufficient_field, refusal_reason, suggestions_count,
citations_cited, citations_invalid, answer_text,
label_correctness, label_evidence_support, reviewer
```

`wiki_*` 四列从任务结果 `Task.result["wiki"]["notes"]` 解析（按 2.3 的子串口径匹配）；`refusal_reason`、`suggestions_count` 只对 `ask` 有值；`label_*` 与 `reviewer` 由人工回填，其余字段脚本自动采集。

---

## 6. 预期结论与讨论

以下均为**待验证的预期**，不是实测结果。每组都写明「什么结果支持什么结论」与「反直觉结果如何解释」。

### 实验一

预期 Wiki 组在「多跳归纳」与「跨库对比」题上正确率更高、证据支持率更高，在「单点事实」题上与分块组持平或略低（二次归纳可能丢失精度）。

- **若 Wiki 全面优于分块**：说明二次整理确实降低了信息碎片度，但需检查是否因为 Wiki 章节更短、更容易被 top-k 覆盖——用「同等证据条数下的正确率」复核。
- **若 Wiki 在事实题上明显变差**：合理，说明归纳过程引入了精度损失。结论应是「Wiki 适合归纳类问题，不适合精确查数」，而不是「Wiki 无用」。
- **若 Wiki 组在库尾题上明显变差**：先查 `wiki_excerpt` 是否被压缩到 120（分片过多导致摘录过短），再查 `wiki_pages_capped` 是否为真（页面数被 60 截断）。这两种情况都是配置造成的，不是方法缺陷。
- **若 Wiki 组几乎无命中**：先查 `plan_route()` 的 `fallback` 是否为 `True`、`notes` 里是否有「未能定位到有效原文片段」的跳过说明，再看库内是否真的生成了页面（`GET /api/kbs/{id}/wiki/tree`）。

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

### 实验五

预期 120 与 0 的整体 Hit@k 差异在 44 题规模下不显著，差异主要集中在「答案被切在块边界」的接缝题上；`overlap=0` 的索引体积最小。

- **若接缝题在 overlap=0 下明显更差**：说明 120 字符重叠确实在救接缝，可作为保留默认值的实证依据。
- **若三组几乎无差异**：说明 700 字符块长 + 标题强制切分已经让接缝问题不突出，可建议把默认 overlap 降到 60 以减少索引体积；但必须给出块数/字符数与 Hit@k 的实测数字，不能只写「影响不大」。
- **若 overlap=240 反而更好**：需要同时报告索引体积增长（块数、总字符、向量调用批次数），否则「更大重叠」会被误读成免费收益。

---

## 7. 威胁与局限

**样本量**。`seed_demo` 三库 25 个检索单元，`seed_expanded` 55 个，扩充到 150–300 后仍属小规模。44 题题集下，正确率差 5 个百分点只对应约 2 道题，落在标注噪声范围内；扩到 60 题后 5 个百分点对应 3 道题。所有对比都应报告置信区间或至少报告逐题明细，不能只给一个百分数。

**单一领域**。现有语料集中在企业人事、客户支持、产品销售三类。结论对其他领域（法律、医疗、工程图纸）的可迁移性未验证。跨领域效果差异主要来自分词与术语分布，本系统的关键词通道用中文二元组 + 英文词（`extract_keywords()`），对专业术语密集的领域可能退化。

**离线向量的局限**。`hash_embed()` 是基于 token 哈希的确定性向量，不含语义信息，只保证链路可跑通。实测查询「量子计算机的退相干时间是多少」（语料中完全无关）仍返回 7 条证据，首位 `coverage = 0.08`。这说明：

- 覆盖率门槛只用于排序让位（`coverage > 0` 的排前面），**不是过滤**，不能作为拒答判据；
- 离线向量下的检索指标不能外推到真实 embedding 场景。

论文主结果必须用配置 B（真实 embedding），配置 A 的结果只能作为「链路可运行」的证据。

**Wiki 的规模约束换了一种形式，但没有消失**。全库覆盖已由分片归纳保证（`tests/test_wiki_scale.py` 用 1000 块验证），剩余上界是三个可配置项：

- `WIKI_MAX_MAP_CALLS = 40`：片数超限会先压缩单块摘录，压到 `WIKI_TOPIC_EXCERPT_MIN = 120` 字仍超就多调用。摘录压到 120 字时，长分块的尾部内容不参与主题归纳（页面生成阶段仍按 `[:1200]` 取原文），归纳质量会下降——这是「成本」与「覆盖」的显式交换，必须在论文里写明，并记录 `wiki_excerpt`。
- `MAX_TOPICS = 12` 是**单片**上限：一个片里若有 20 个不同主题，只能出 12 个。片越大（`WIKI_SHARD_CHAR_BUDGET` 越大）这个损失越明显，所以「分片更少」不等于「效果更好」。
- `WIKI_MAX_PAGES = 60`：合并后按证据数量保留前 60 个主题，长尾主题只剩分块检索。触发时会写 notes，实验必须记录 `wiki_pages_capped`。

**Reduce 只能合并「看起来像同一件事」的主题**。合并判据（`wiki._should_merge()`）是标题归一化 + 包含关系 + 数字序列 + 相似度/证据重叠。跨片同义但表述差异大的主题（「住宿标准」vs「出差住宿报销额度」）数字序列相同、相似度可能低于 0.45，会各自成页，导致 Wiki 页面数膨胀并稀释检索命中。这是当前合并策略的已知上限，改进方向是引入 embedding 聚类，但会引入新的成本项。

**人工判定的主观性**。「部分正确」这一档的边界天然模糊——漏掉一个限定条件算部分正确还是错误，不同标注者会有分歧。缓解方式：两人独立标注 + 计算 Cohen's κ，κ < 0.6 时重新对齐判定标准再标。三档标准必须在标注开始前写死，中途不得修改。

**单机环境的外部效度**。所有耗时数据来自单机、单进程、SQLite。多人并发场景下的锁竞争（`busy_timeout=15000`）、Worker 队列积压、以及 Electron 与 sidecar 的进程开销都未测量。P95 延迟在生产并发下会显著高于实验值。

**`top_k` 的语义混用**。`retrieve(top_k=N)` 里 N 同时是「每库候选数」（`retrieval.py:596`）和「最终条数上限」（`retrieval.py:665`，且被 `max(top_k, 3)` 兜底）；`/api/search/preview` 根本不接受 `top_k`。做 Hit@k 实验时若直接用 `top_k=k`：单库时 k=1 只返回 1 条、k=2 返回 2 条（都凑不满 3），而**多库（≥3 个）时 k=1 与 k=2 都会返回 3 条**，会得出「k 不影响效果」的错误结论。要严格控制返回条数，只能从结果里自己截断。

**拒答判定的保守偏置**。开头 80 字命中拒答语句即判拒答，会误伤「半答半拒」的回答（先拒一句再补一半）。这可能让无答案题的召回偏高、可答题的「部分正确」偏低。实验设计上必须把「拒答判定」与「回答质量」分开标注，不能用一个 `insufficient` 字段同时代表两件事。

**成本口径的剩余盲区**。`CostRecord` 已覆盖任务、Wiki 编辑、失败任务（`kind="failed_attempt"`），但连接测试（`POST /api/settings/models/{mid}/test`）不写记录——它不属于任何库的整理代价。若实验里用连接测试探活，这部分开销不会出现在 `/cost` 里，需单独记录。另外 `cost.record()` 对全零 stats 直接跳过，因此「零成本任务」不会出现在 `records` 里，统计任务条数时不能拿 `record_count` 当任务数。

---

## 8. 实验结果（配置 A 离线，实测）

> 本节是 P0-1「补齐实验数据并落库」的产出。原始数据、汇总表、数据字典与复现命令
> 全部在 `docs/results/`，采集器为 `backend/scripts/run_experiments.py`。
> **本机无在线模型配置与凭据**（环境变量无模型密钥、无 `data/` 下的 `ModelConfig`，
> `GET /api/meta` 实测 `offline_models=true`），因此本轮只能跑 §1.2 的**配置 A 离线**：
> 向量为 `local-hash-embedding:256`、`channel_weights()` 为 `[1.0, 0.45]`、
> 生成侧为抽取式。按 §7 与 `docs/THESIS.md` §6.7，离线结果只证明「链路可运行、
> 字段口径正确、结果可复现」，**不是论文主结果**；配置 B 的指标一律「待实验」。
> 按 `:3` 的约定，凡未实测的数字不填推测值。

### 8.1 环境与配置（实测，补 §1.1 的留白）

| 项 | 实测值 |
| --- | --- |
| 操作系统 | Windows 11（10.0.26200），AMD64 |
| Python | 3.13.11（本机全局解释器，本 checkout 无 `.venv`） |
| 后端 / 版本 | FastAPI + Uvicorn 单进程，`v1.0.17` |
| 数据库 | SQLite 单文件，临时目录，跑完即弃 |
| CPU | 16 逻辑核；**具体型号本机不可得**（虚拟机把 `Win32_Processor.Name` 与注册表 `ProcessorNameString` 都报为 `Not Available`，厂商仅 `GenuineIntel`），如实记录，不填推测值 |
| 内存 | 31.78 GB |
| 磁盘 | WD PC SN540 SDDPNPF-512G |

完整读数（含 `git_head`、全部 `WRK_*` 参数取值）在 `docs/results/environment.json`。
§1.1 里「CPU、内存、磁盘型号待实测」的留白由此补齐；仍取不到的 CPU 型号保持留白。

固定参数取自 `backend/app/config.py`：`DEFAULT_CHUNK_SIZE=700`、
`DEFAULT_CHUNK_OVERLAP=120`、`TABLE_CHUNK_BUDGET=1600`、`RRF_K=60`、
`PER_KB_CANDIDATES=8`、`MAX_EVIDENCE=10`、`CONTEXT_CHAR_BUDGET=9000`、
`WIKI_MAX_MAP_CALLS=40`、`WIKI_MAX_PAGES=60`。语料为 `seed_expanded --reset`
的三库 55 个检索单元（19/20/16）；题集为 `demo_data/questions.json` 44 题
（`fact`/`cross`/`multi_hop` 各 11，`no_answer` 11）。Wiki 由离线抽取式生成，
`wiki._plan_shards()` 只读探针实测三库均为 `shards=1`、`excerpt=900`；
`WikiBuildResult.notes` 为空，按 §2.3 可推断「单片归纳、未截断、未跳过」。

### 8.2 采集方式与命令

主采集器 `backend/scripts/run_experiments.py` 在进程内用
`fastapi.testclient.TestClient` 打真实路由 `POST /api/search/preview`
（与独立起 uvicorn 共用路由层、`get_db`、`CallStats` 的 usage 组装），
不改产品代码；E3 的四组权重、E5 的四组 overlap 通过进程内打补丁切换，
E4 按 §3 实验四用探针复现。

```bash
# 一键重跑全部结果（仓库根目录，不需要起服务、不需要凭据）
PYTHONIOENCODING=utf-8 python backend/scripts/run_experiments.py

# 表格分块数字独立复核
PYTHONIOENCODING=utf-8 python backend/scripts/inspect_chunks.py
```

既有的 `backend/scripts/collect_metrics.py` 本轮也做了一次**独立 HTTP 路径复核**
（命令与全部参数见 `docs/results/README.md` §3.2，结果在
`docs/results/e2_http_crosscheck.csv`，132 行 = 44 题 × 3 次）：

```bash
PYTHONIOENCODING=utf-8 python backend/scripts/collect_metrics.py \
  --base-url http://127.0.0.1:8765 \
  --username admin --password '<本地临时口令>' \
  --input docs/results/questions.search.json \
  --kb-ids 1,2,3 \
  --mode search --repeat 3 \
  --label e2-multi-http-basic \
  --out docs/results/e2_http_crosscheck.csv
```

（本轮用全局 Python 3.13.11 实测：当前 checkout 无 `.venv`；若用仓库自带的
`.venv`，把 `python` 换成 `.venv/Scripts/python.exe` 即可。）

复核结果：真实 HTTP 路径得到的 33 道可答题均值
（`evidence_count=9.5152`、`top1_coverage=0.3399`、`mean_coverage=0.1448`、
`calls=1.0`、`embed_calls=1.0`、`offline_calls=1.0`）与主采集器 E2 多库组逐位一致，
两条采集路径口径相同。`questions.search.json` 由 `demo_data/questions.json`
转换而来，转换命令也写在 `docs/results/README.md` §3.2。

### 8.3 指标口径

Hit@k / MRR / 覆盖率以 `docs/results/expected_units.json` 的「预期命中单元」
为基准（用 `answer_keywords` 自动标注，规则见采集器 `derive_expected()`）；
分母为 33 道可答题，11 道 `no_answer` 题不参与 Hit@k。
`/api/search/preview` 不接受 `top_k`，Hit@1/3/5 由脚本自行截取 `evidence[]`。
`latency_ms` 为接口端到端墙钟时间，不含网络往返。四个逐题 CSV 的前 27 列
与 `collect_metrics.py` 的 `FIELDS` 同名同序，扩展列含义见
`docs/results/README.md` §4。

### 8.4 五个实验的实测结果

下表只放聚合值；每个数字都能在对应 CSV 的某一行复算，聚合规则见
`docs/results/summary.json`，人读版见 `docs/results/summary.md`。

**实验一：原文分块 vs Wiki 增强**（`e1_wiki_vs_basic.csv`，同一批分块只切 `mode`；
`route_used=wiki|wiki|wiki`、`fallback=False` 已核对）

| 组 | Hit@1 | Hit@3 | Hit@5 | Hit@10 | MRR | 证据数 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| wiki | 0.8182 | 0.9091 | 1.0 | 1.0 | 0.8833 | 8.8788 |
| basic | 0.8485 | 0.9394 | 0.9697 | 1.0 | 0.9043 | 9.5152 |

**实验二：单库 vs 多库**（`e2_single_vs_multi.csv`）

| 组 | Hit@1 | Hit@3 | Hit@5 | Hit@10 | MRR | 证据数 | 候选数 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| single | 0.8485 | 0.9697 | 1.0 | 1.0 | 0.9101 | 5.1212 | 5.1212 |
| multi | 0.8485 | 0.9394 | 0.9697 | 1.0 | 0.9043 | 9.5152 | 14.0606 |

**实验三：四组通道消融**（`e3_rrf_ablation.csv`；纯向量组同时把
`vector_rank` 的 `lexical_ids` 置 `None`）

| 组 | 权重 | Hit@1 | Hit@3 | Hit@10 | MRR |
| --- | --- | ---: | ---: | ---: | ---: |
| weighted_rrf | `[1.0,0.45]` | 0.8485 | 0.9394 | 1.0 | 0.9043 |
| equal_rrf | `[1.0,1.0]` | 0.8485 | 0.9394 | 1.0 | 0.9043 |
| keyword_only | `[1.0,0.0]` | 0.8788 | 0.9697 | 1.0 | 0.9318 |
| vector_only | `[0.0,1.0]` | 0.6970 | 0.8485 | 0.8788 | 0.7550 |

**实验四：表格分块三策略**（`e4_table_chunking.csv`，探针实测；
`tail` 列指表格末尾行的题目）

| 文件 | 策略 | 块数 | 总字符 | min/avg/max | 向量批次 | 末尾行在上下文 | 末尾题答案进上下文 |
| --- | --- | ---: | ---: | --- | ---: | ---: | --- |
| 700行.docx | row | 700 | 58688 | 76/83.84/93 | 44 | 1 | 3/3 |
| 700行.docx | whole | 1 | 59403 | 59403/59403.0/59403 | 1 | 0 | 0/3 |
| 700行.docx | group | 39 | 60546 | 1385/1552.46/1560 | 3 | 1 | 3/3 |
| 120行.docx | row / whole / group | 120 / 1 / 7 | 10066 / 10201 / 10384 | 76/83.88/93 · 10201 · 1055/1483.43/1558 | 8 / 1 / 1 | 1 / 0 / 1 | 3/3 · 0/3 · 3/3 |
| 200行.xlsx（薪酬明细） | row / whole / group | 200 / 1 / 12 | 16760 / 16979 / 17356 | 76/83.8/93 · 16979 · 193/1446.33/1564 | 13 / 1 / 1 | 1 / 0 / 1 | 3/3 · 0/3 · 3/3 |

`group` 策略数字与 `inspect_chunks.py` 一致。整表策略在「检索命中」上不差
（单块就含全部行），但 `build_context()` 按 9000 字符预算把 1 万～5.9 万字符
的整表块截断，末尾行进不了提示词——这是 §3 实验四要的截断失效证据。

**实验五：overlap 消融**（`e5_overlap.csv` + `e5_seam_probe*.csv`）

| overlap | 44 题题集块数 | Hit@1 | Hit@3 | Hit@10 | MRR | 接缝探针块数 | 总字符 | 同块相邻锚点对 | 接缝对命中率 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| 0 | 55 | 0.8182 | 1.0 | 1.0 | 0.8990 | 8 | 5336 | 24/31 | 0.7742 |
| 60 | 55 | 0.8182 | 1.0 | 1.0 | 0.8990 | 11 | 5943 | 31/31 | 1.0 |
| 120 | 55 | 0.8182 | 1.0 | 1.0 | 0.8990 | 11 | 6543 | 31/31 | 1.0 |
| 240 | 55 | 0.8182 | 1.0 | 1.0 | 0.8990 | 15 | 8703 | 31/31 | 1.0 |

44 题题集上四组分块数同为 55，因为 `demo_data` 段落均短于 700 字符，
不触发 `split_blocks` 的「同一标题内长度溢出」分支（§1.3 已说明重叠只在
该分支与超长段落句级回退上生效）。为单独测出机制，另用 32 段合成受控语料
做接缝探针（逐对明细见 `e5_seam_probe_detail.csv`）：`overlap=0` 时
相邻锚点对只有 `24/31` 落在同一块，`60/120/240` 均为 `31/31`，
同时块数与总字符随重叠增大。注意 `overlap=0` 也关掉了超长段落分支的
「回退 1 句」（§3 实验五），两种机制不可混为一谈。

**检索耗时**（`latency.csv`，9 题 × 10 次 = 90 样本，配置 A 离线）：
min / P50 / P95 / max / 均值 / 标准差由采集器写入 `docs/results/summary.json`
并由 `docs/results/summary.md` 打印，本处不写死数字——耗时随本机负载波动，
重跑会变，以结果文件当次读数为准；接入真实 embedding 后必须重测。

### 8.5 只在离线口径下成立的观察

- E1 离线抽取式 Wiki 未带来 Hit@1 增益（0.8182 < 0.8485），Hit@10 两组都到 1.0；
  证据可回挂、路由无回退。**不能据此写「Wiki 无用」**——§6 实验一的预期本来就
  要求配置 B 下重测。
- E2 单库组 Hit@3 更高、多库组证据数更多，符合「跨库融合把正确证据挤到后面」；
  噪声已逐题落在 `noise_unrelated_zero_cov` 列。
- E3 纯关键词最好、纯向量最差，正是 §6 实验三写明的「中文小语料 + 离线哈希
  向量下纯关键词优于全部融合方案」的负结果，如实报告。
- E5 结论符合 §3 实验五的判定标准：整体 Hit@k 差异在噪声内，接缝类差异明显，
  且更大 overlap 有索引体积代价（块数 8→15、总字符 5336→8703）。

### 8.6 待实验（§6 的预期仍是预期，未被本节替换）

配置 B（真实 embedding + 在线生成）下 E1–E5 的全部指标；正确性三档、
证据支持率、引用精确率、拒答正确率（需两人独立标注 + Cohen's κ）；
Wilcoxon 符号秩 / McNemar 检验的实际 p 值；真实 embedding 下的耗时 P50/P95；
150–300 检索单元扩料题集的库尾题与接缝题人工标注；
在线模型下 `live_calls` / `total_tokens` / `latency_ms` 的真实读数。
这些项在 `docs/THESIS.md` §4 与 `docs/results/README.md` §8 同步标注「待实验」。


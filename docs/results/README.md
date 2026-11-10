# 五个对照实验结果（第一波 / P0-1）

本目录是 `docs/HEALTH_CHECK.md` §4.1 **P0-1「补齐实验数据并落库」** 的产出：
把 `docs/EXPERIMENT.md` §3 的 E1–E5 五个对照实验跑出**可复现的原始结果**，
逐题落 CSV、逐实验汇总，并在 `docs/EXPERIMENT.md`、`docs/THESIS.md` 里回填实测值。

---

## 1. 先说清楚：本轮是「配置 A 离线」

**本机没有可用于在线实验的模型配置与凭据**（体检时的开放问题）：

- 环境变量中没有 `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `WRK_*` 模型密钥；
- 仓库 checkout 没有 `data/`，因此没有持久化的 `ModelConfig` / `Provider`；
- `GET /api/meta` 实测 `offline_models=true`、`llm_configured=false`、
  `embedding_configured=false`，向量签名为 `local-hash-embedding:256`，
  `channel_weights()` 为 `[1.0, 0.45]`。

按 `docs/EXPERIMENT.md` §1.2 的三选一，本轮只能跑**配置 A 离线**；
按 `docs/EXPERIMENT.md` §7 与 `docs/THESIS.md` §6.7，**离线哈希向量与抽取式回答
只证明链路可运行，不能作为论文主结果**。因此本目录所有数字的有效范围写死为：

> 证明五个实验的采集链路可运行、字段口径正确、结果可复现；
> **不作为论文主结果**。配置 B（真实 embedding + 在线生成）的全部指标仍为「待实验」。

`docs/EXPERIMENT.md:3` 的约定「凡未能实测的一律标注『待实验』，不填推测值」在本轮严格执行：
正确性三档、证据支持率、引用精确率、拒答正确率、Wilcoxon/McNemar 的 p 值、
真实 embedding 下的耗时，全部保持「待实验」，没有用离线数字冒充。

---

## 2. 环境与配置（实测）

见 `environment.json`。要点：

| 项 | 实测值 |
| --- | --- |
| 操作系统 | Windows 11（10.0.26200），AMD64 |
| Python | 3.13.11（本机全局解释器；本 checkout 无 `.venv`） |
| 后端 | FastAPI + Uvicorn，`v1.0.17` |
| 数据库 | SQLite 单文件，临时目录，跑完即弃 |
| CPU | 16 逻辑核；**具体型号本机不可得**（虚拟机把 `Win32_Processor.Name` 与注册表 `ProcessorNameString` 都报为 `Not Available`，厂商仅 `GenuineIntel`），已如实记录，不填推测值 |
| 内存 | 31.78 GB |
| 磁盘 | WD PC SN540 SDDPNPF-512G |
| 代码版本 | `environment.json.git_head`（采集时所在提交；本轮提交会在其后） |
| 向量来源 | `local-hash-embedding:256`（无真实 embedding） |
| 通道权重 | `[1.0, 0.45]` |
| 随机性 | 离线哈希向量确定性，无随机种子；重复运行除耗时外结果一致 |

固定参数取自 `backend/app/config.py`：`DEFAULT_CHUNK_SIZE=700`、
`DEFAULT_CHUNK_OVERLAP=120`、`TABLE_CHUNK_BUDGET=1600`、`RRF_K=60`、
`PER_KB_CANDIDATES=8`、`MAX_EVIDENCE=10`、`CONTEXT_CHAR_BUDGET=9000`、
`WIKI_MAX_MAP_CALLS=40`、`WIKI_MAX_PAGES=60`、`WORKER_CONCURRENCY=1`。

语料：`seed_expanded --reset`，三库共 **55 个检索单元**（19 / 20 / 16）。
题集：`demo_data/questions.json`，44 题（`fact` / `cross` / `multi_hop` 各 11，
`no_answer` 11）。Wiki 由离线抽取式生成，三库各 1 个分片
（`wiki_shards=1`、`wiki_excerpt=900`，由 `wiki._plan_shards()` 只读探针实测；
`WikiBuildResult.notes` 为空，按 §2.3 可推断「单片归纳、未截断、未跳过」）。

---

## 3. 怎么复现

### 3.1 一键重跑全部结果（推荐）

```bash
# 仓库根目录；不需要起服务、不需要凭据、不需要改产品代码
PYTHONIOENCODING=utf-8 python backend/scripts/run_experiments.py
```

`backend/scripts/run_experiments.py` 做的事：

1. 在系统临时目录建隔离的 `WRK_DATA_DIR` / `WRK_DB_PATH` / `WRK_SECRET_DIR`，
   设 `WRK_ADMIN_PASSWORD`（仅临时库的本地口令）、`WRK_WORKER_ENABLED=0`；
2. 调 `scripts.seed_expanded.main()` 灌数据（真实解析器 + 分块器 + 离线哈希向量）；
3. 用 `fastapi.testclient.TestClient` 在**同一进程内**打真实路由
   `POST /api/search/preview`（与独立起 uvicorn 共用路由层、`get_db`、
   `CallStats` 的 usage 组装），逐题落 CSV；
4. E3 的四组权重、E5 的四组 overlap 通过**进程内打补丁**切换，不改产品代码
   （E3 按 `docs/EXPERIMENT.md` §3 实验三的说明，本来就没有配置开关）；
5. E4 按实验方案要求用探针复现（`split_table` / `_row_to_line` /
   `keyword_rank` / `vector_rank` / `rrf_fuse` / `build_context` 均为产品函数）；
6. 结果写入本目录，临时库留在系统临时目录、不写仓库 `data/`。

### 3.2 用 `collect_metrics.py` 在真实起服务时复核（本轮已实跑）

`backend/scripts/collect_metrics.py` 是仓库既有的 HTTP 采集脚本。本轮用它做了
一次**独立 HTTP 路径复核**，结果见 `e2_http_crosscheck.csv`（132 行 = 44 题 × 3 次）。

```powershell
# 1) 隔离实验数据目录
$env:WRK_DATA_DIR = "$env:TEMP\wrk_p01_http\data"
$env:WRK_DB_PATH = "$env:TEMP\wrk_p01_http\data\db\app.sqlite3"
$env:WRK_SECRET_DIR = "$env:TEMP\wrk_p01_http\data\secrets"
$env:WRK_ADMIN_PASSWORD = "<本地临时口令>"
$env:WRK_WORKER_ENABLED = "0"
$env:PYTHONIOENCODING = "utf-8"

# 2) 灌数据（在 backend 目录）
python -m scripts.seed_expanded --reset

# 3) 起服务（在 backend 目录）
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765

# 4) 把 1 号库切成 basic，使三库口径与 E2/E3/E5 基线一致
#    （seed 默认 1 号库为 wiki；不切的话复核结果对应的是 E1 wiki 组）
$tok = (Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/auth/login' -Method Post `
  -ContentType 'application/json' `
  -Body '{"username":"admin","password":"<本地临时口令>"}').token
Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/kbs/1' -Method Patch `
  -Headers @{Authorization="Bearer $tok"} -ContentType 'application/json' `
  -Body '{"mode":"basic"}'

# 5) 采集（仓库根目录）
python backend/scripts/collect_metrics.py `
  --base-url http://127.0.0.1:8765 `
  --username admin --password "<本地临时口令>" `
  --input docs/results/questions.search.json `
  --kb-ids 1,2,3 `
  --mode search --repeat 3 `
  --label e2-multi-http-basic `
  --out docs/results/e2_http_crosscheck.csv
```

`questions.search.json` 由题集转换而来（本轮已生成并提交）：

```bash
python -c "import json; json.dump([q['question'] for q in json.load(open('demo_data/questions.json',encoding='utf-8'))], open('docs/results/questions.search.json','w',encoding='utf-8'), ensure_ascii=False, indent=2)"
```

**复核结果**：`collect_metrics.py` 走真实 HTTP 得到的 33 道可答题均值
（`evidence_count=9.5152`、`top1_coverage=0.3399`、`mean_coverage=0.1448`、
`calls=1.0`、`embed_calls=1.0`、`offline_calls=1.0`）与
`run_experiments.py` 的 E2 多库组**逐位一致**，证明两条采集路径口径相同。

### 3.3 表格分块的独立复核

```bash
PYTHONIOENCODING=utf-8 python backend/scripts/inspect_chunks.py
```

打印各 fixture 的解析块数、表格数、分块数、字符 min/avg/max 与行区间。
本轮 `e4_table_chunking.csv` 的 `group` 策略数字与它一致（700 行 → 39 块、
1385–1560 字符；120 行 → 7 块；200 行 xlsx → 13 块）。

---

## 4. 文件清单与数据字典

| 文件 | 内容 |
| --- | --- |
| `summary.md` | 人读汇总表（五个实验 + E4 表格 + E5 接缝 + 耗时 + 待实验清单） |
| `summary.json` | 机读汇总，含各实验分组聚合、E4/E5 明细、耗时、Wiki 协变量、环境 |
| `e1_wiki_vs_basic.csv` | E1 逐题原始行（44 题 × 2 组 = 88 行） |
| `e2_single_vs_multi.csv` | E2 逐题原始行（44 题 × 2 组 = 88 行） |
| `e3_rrf_ablation.csv` | E3 逐题原始行（44 题 × 4 组 = 176 行） |
| `e5_overlap.csv` | E5 逐题原始行（44 题 × 4 组 = 176 行） |
| `e4_table_chunking.csv` / `.json` | E4 每个「文件 × 表 × 策略」一行（12 行） |
| `e5_seam_probe.csv` | E5 接缝探针汇总（4 个 overlap 各一行） |
| `e5_seam_probe_detail.csv` | 接缝探针逐对明细（4 × 31 行） |
| `e2_http_crosscheck.csv` | `collect_metrics.py` 真实 HTTP 复核（132 行） |
| `latency.csv` | 离线检索耗时逐次样本（9 题 × 10 次 = 90 行） |
| `expected_units.json` | 44 题的预期命中单元自动标注（逐题可追溯） |
| `questions.search.json` | 44 题纯文本列表，供 `collect_metrics.py` 使用 |
| `environment.json` | 实测硬件、版本、配置、代码提交 |

### CSV 列

`e1/e2/e3/e5` 四个 CSV 的前 27 列与 `collect_metrics.py` 的 `FIELDS` **同名同序**
（`run_label, repeat, kb_ids, kb_modes, question, mode, evidence_count,
top1_coverage, mean_coverage, source_types, answer_chars, citation_count,
invalid_citations, insufficient, latency_ms, model_latency_ms, calls,
chat_calls, embed_calls, live_calls, offline_calls, prompt_tokens,
completion_tokens, embed_tokens, total_tokens, stats_mode, embed_mode, error`），
后面是本轮扩展列。常用扩展列：

| 列 | 含义 |
| --- | --- |
| `experiment` / `group` / `config` | 实验编号、分组、该组配置说明 |
| `question_id` / `question_type` / `scored` | 题号、类型、是否计入指标分母（`no_answer` 题为 0） |
| `expected_unit_count` | 该题自动标注出的预期命中单元数 |
| `hit_1/3/5/10` / `mrr` / `first_hit_rank` | 前 k 条证据是否命中预期单元、MRR、首个命中名次 |
| `top1_evidence_id` / `coverage_top1` | 首位证据的 `evidence_id` 与 `coverage` |
| `candidate_total` / `final_total` / `rrf_k` | `route.stats` 的候选数、最终条数、RRF 常数 |
| `route_used` / `route_fallback` | 每个库的路由结果（E1 用于确认 wiki 组确实走 Wiki 且未回退） |
| `noise_unrelated_zero_cov` | 最终证据里「`coverage==0` 且来自非预期库」的条数（E2 噪声） |
| `channel_weights` / `lexical_gate` | E3 该组实际权重、向量通道是否受关键词门控 |
| `overlap` / `chunk_count` / `total_chars` / `avg_len` / `vector_batches` | E5 自变量与索引规模 |
| `wiki_shards` / `wiki_excerpt` / `wiki_pages_capped` / `wiki_notes_present` | E1 的 Wiki 协变量（§2.3） |
| `error` | 单条采集失败原因（本轮四个 CSV 均为 0 条错误） |

E4 的列：`source_file, table_caption, strategy, total_rows, cols, chunk_count,
total_chars, min_len, avg_len, max_len, vector_batches, locator_granularity,
ranges_contiguous, tail_row_in_context, answer_in_top10_head/tail,
answer_in_context_head/tail, probe_questions`（`probe_questions` 是 6 道探针题的
逐题 JSON：行号、工号、头/尾位置、答案单元索引、是否进 top-10、是否进上下文）。

---

## 5. 指标口径（重要）

- **Hit@k / MRR / 覆盖率**：以 `expected_units.json` 的「预期命中单元」为基准。
  判据为「前 k 条证据的 `chunk_id`（Wiki 证据用回挂的 `evidence[].chunk_id`）
  落在预期单元集合里」。预期单元由 `answer_keywords` 自动标注
  （按 `|` 拆片段、去空白与全角差异，某分块包含某条关键词的全部片段即入选），
  规则写在 `run_experiments.py derive_expected()`，逐题可追溯到 CSV 的
  `expected_unit_count` 与 `expected_units.json`。
- **分母**：33 道可答题（`fact` 11 + `cross` 11 + `multi_hop` 11）。
  11 道 `no_answer` 题没有预期单元，按 `docs/EXPERIMENT.md` §4.1 不参与 Hit@k，
  其正确性需人工判定 → 待实验。
- **`top_k` 陷阱**：`/api/search/preview` 不接受 `top_k`，固定返回 `MAX_EVIDENCE=10`；
  Hit@1/3/5 由脚本从 `evidence[]` 自行截取，不依赖接口参数（`docs/EXPERIMENT.md` §4.3）。
- **耗时**：`latency_ms` 是 in-process 接口端到端墙钟时间（含路由/DB/序列化），
  不含网络往返；P50/P95 来自 `latency.csv` 的 90 个样本。
- **成本**：`calls` 恒为 1（一次查询向量化）、`live_calls` 恒为 0（离线哈希），
  `stats_mode=no-chat`（只检索不生成）。这些是配置 A 的真实读数，
  **不能与配置 B 的费用结论混用**。

---

## 6. 五个实验的分组与控制

| 实验 | 自变量 | 控制 | 产出 |
| --- | --- | --- | --- |
| E1 | `KnowledgeBase.mode`：`wiki` / `basic` | **同一库、同一批分块**，只切 `mode`；三库 Wiki 均由离线抽取式生成；`route_used=wiki\|wiki\|wiki`、`fallback=False` 已核对 | `e1_wiki_vs_basic.csv` |
| E2 | `kb_ids` 单元素（按 `expected_kb[0]`） vs 三元素 | 同一题集、三库均为 `basic` | `e2_single_vs_multi.csv` |
| E3 | 加权 RRF `[1.0,0.45]` / 等权 `[1.0,1.0]` / 纯关键词 `[1.0,0.0]` / 纯向量 `[0.0,1.0]` | 同一语料、同一题集、三库 `basic`；纯向量组同时把 `vector_rank` 的 `lexical_ids` 置 `None`，避免测成「关键词门控后的向量」 | `e3_rrf_ablation.csv` |
| E4 | 逐行 / 整表 / 行组+表头重复 | 三份表格 fixture；`row`/`whole` 在探针里复现 | `e4_table_chunking.csv` |
| E5 | `WRK_CHUNK_OVERLAP ∈ {0,60,120,240}` | 同一 10 份语料、同一题集；每组独立临时库并各自重算预期单元 | `e5_overlap.csv` + `e5_seam_probe*.csv` |

E3 的后三组与 E5 的换挡都是**进程内打补丁**，不是产品配置开关；
`docs/EXPERIMENT.md` §3 实验三已明确「不要在论文里声称『通过配置即可切换』」，本轮照办。

---

## 7. 结果速览与结论（配置 A 离线）

完整数字见 `summary.md` / `summary.json`。几条只在离线口径下成立的观察：

- **E1**：Wiki 组 Hit@1 `0.8182` 略低于分块组 `0.8485`，Hit@10 两者都到 `1.0`；
  离线抽取式 Wiki 没有带来增益，但证据可回挂、路由无回退。**不能据此下「Wiki 无用」的结论**。
- **E2**：单库组 Hit@3 `0.9697` 高于多库组 `0.9394`，多库组证据数更多（9.52 vs 5.12），
  符合「跨库融合把正确证据挤到后面」的预期；噪声列 `noise_unrelated_zero_cov` 已逐题落盘。
- **E3**：纯关键词最好（Hit@1 `0.8788`、MRR `0.9318`），纯向量最差
  （Hit@1 `0.6970`、MRR `0.7550`）。这正是 `docs/EXPERIMENT.md` §6 实验三写明的
  「中文小语料 + 离线哈希向量下，纯关键词优于全部融合方案完全可能」的负结果，
  如实报告，不掩盖。
- **E4**：整表策略检索命中不差（单块含全部行），但 `tail_row_in_context=0`、
  末尾题 `answer_in_context_tail=0/3`——`build_context()` 按 9000 字符预算
  把 1 万～5.9 万字符的整表块截断，末尾行进不了提示词。逐行/行组均为 `3/3`。
- **E5**：44 题题集上四组分块数同为 55、Hit@k 完全相同，因为 `demo_data`
  段落均短于 700 字符，不触发 `split_blocks` 的「同一标题内长度溢出」分支；
  接缝探针（32 段合成受控语料）单独测出机制：`overlap=0` 时相邻锚点对
  只有 `24/31` 落在同一块，`60/120/240` 均为 `31/31`，同时块数从 8 增至 11/11/15、
  总字符从 5336 增至 5943/6543/8703。即「overlap 只对接缝类问题有效，
  对整体指标影响在噪声内」，且更大 overlap 有索引体积代价。
- **检索耗时**：`latency.csv` 90 样本的 min / P50 / P95 / max / 均值 / 标准差
  见 `summary.md` 与 `summary.json`。耗时随本机负载波动、重跑会变，
  以结果文件当次读数为准；接入真实 embedding 后必须重测。
- 上述每个数字都能在对应 CSV 的某一行/列复算；聚合规则见 `summary.json`。

---

## 8. 待实验（不填推测值）

- 配置 B（真实 embedding + 在线生成）下 E1–E5 的全部指标，含论文主结果；
- 正确性三档、证据支持率、引用精确率、拒答正确率（需两人独立标注 + Cohen's κ）；
- Wilcoxon 符号秩 / McNemar 检验的实际 p 值；
- 真实 embedding 下的检索耗时 P50/P95；
- 150–300 检索单元的扩料题集、库尾题与接缝题的人工标注；
- 成本 `live_calls` / `total_tokens` / `latency_ms` 在在线模型下的真实读数。

---

## 9. 与第二波的边界

本轮只做 P0-1 相关改动：**新增 `docs/results/`、新增 `backend/scripts/run_experiments.py`、
在 `docs/EXPERIMENT.md` 增「实验结果」章、在 `docs/THESIS.md` §4 回填实测值**。

`README.md` / `docs/DESIGN.md` / `docs/HANDOFF.md` / `docs/API.md` 以及
「全仓文档事实口径统一」（版本号、表数、`WORKER_CONCURRENCY`、`busy_timeout`、
测试基线、`文件:行` 锚点）留给第二波，本轮**一个字都没改**，避免两边改同一批文件。
本轮也没有修改任何产品代码（`backend/app/**`）。

> **第二波已完成（补记）**：上述统一工作落在 `docs/FACTS.md`（唯一事实口径）、
> `docs/HANDOFF.md`（v1.0.17 当前状态首屏）、`docs/evidence/pytest-baseline.txt`
> 与 `docs/archive/HANDOFF_HISTORY.md`。本目录 16 个结果文件、
> `docs/EXPERIMENT.md` §8、`docs/THESIS.md` §4 以及所有「待实验」标注均原样保留。

# 统一事实口径（单一事实源）

> 本文件是 `README.md`、`docs/DESIGN.md`、`docs/EXPERIMENT.md`、`docs/THESIS.md`、
> `docs/HANDOFF.md`、`docs/API.md` 六份文档的**唯一事实口径**。
> 依据：体检清单 `docs/HEALTH_CHECK.md` §3.1、§3.2（D1–D7）与 §4 的 P0-2 / P0-3 / P0-4。
> 原则：**代码是唯一事实源**；数字只写在本文件，其余文档只放链接，不再各自写死。

---

## 0. 为什么有这份文件

体检结论（§2、§3.1、§3.2）指出：项目代码本身是健康的，短板在「交付证据链」——
六份文档把同一批事实写成了互不相同的版本：

- 版本号：`README.md` 写 `1.0.17`，`docs/THESIS.md` 写 `1.0.0`，`docs/HANDOFF.md` 首屏停在 `v1.0.10`；
- 数据表：`README.md` 写 21 张，`docs/THESIS.md` 写 19 张；
- Worker 并发、`busy_timeout`：五份文档写 `2` 与 `15000`，而代码是 `1` 与三档；`docs/DESIGN.md` 内部一处 `15000`、一处 `3s`，自相矛盾；
- 测试基线：六份文档分别写 535 / 157 / 192 / 200 / 171 / 93 / 82 / 59 个 passed，全仓无可信基线；
- `文件:行` 引用大面积漂移（§3.2 D7、§5.3 抽查 8 处、4 处指错）。

本文件把这些值收敛到一处。**历史快照里的旧数字没有删除**，而是连同当时的上下文
整体移入 `docs/archive/HANDOFF_HISTORY.md`，并在文件头标明「历史记录，不是当前事实」。

---

## 1. 权威事实表（逐条以代码为准）

| 项 | 当前值 | 代码依据（文件 + 符号名） | 体检前被写成 |
| --- | --- | --- | --- |
| 应用版本 | `1.0.17` | `backend/app/main.py` 的 `VERSION`；`frontend/package.json` 的 `version` | THESIS.md `1.0.0`、HANDOFF.md 首屏 `v1.0.10` |
| 数据表数量 | **21 张** | `backend/app/models.py` 的 21 个 `__tablename__` | THESIS.md 19 张 |
| Worker 并发 | **1** | `backend/app/config.py` 的 `WORKER_CONCURRENCY`（`WRK_WORKER_CONCURRENCY` 默认 `"1"`） | `2`（5 份文档） |
| SQLite 写锁等待 | **三档 3s / 30s / 1s**（见 §1.1） | `backend/app/config.py` 的 `BUSY_TIMEOUT_INTERACTIVE` / `BUSY_TIMEOUT_WORKER` / `BUSY_TIMEOUT_PROGRESS` | 一刀切 `busy_timeout=15000`；DESIGN.md §4.5 又写 `3s` |
| 拿不到锁的重试次数 | `5`（`WRK_LOCK_RETRY_ATTEMPTS`，兼容别名 `WRK_LOCK_RETRY`） | `backend/app/config.py` 的 `LOCK_RETRY_ATTEMPTS` | 未进文档 |
| 可调项 | `config.py` 中 **38 个 `WRK_*`** 环境变量（另有若干由 `desktop_entry.py` / `security.py` / `answering.py` 读取） | `backend/app/config.py` | README §5 漏列 11 个（见 §1.2） |
| 后端测试基线 | **583 passed, 1 skipped**（见 §2） | 一次真实全量运行，原始输出 `docs/evidence/pytest-baseline.txt` | 六份文档 8 个互不相同的数字 |

> 表里「当前值」一列在 2026-11 本轮逐条回查代码确认；D1–D7 的结论在代码里**仍然成立**
> （`config.py:26` 仍是 `1`，`config.py:34-40` 仍是三档，`main.py:36` 仍是 `1.0.17`，
> `models.py` 仍是 21 个 `__tablename__`）。

### 1.1 SQLite 写锁三档（替代旧的一刀切 `15000ms`）

| 档 | 变量 | 环境变量 | 默认 | 用途 |
| --- | --- | --- | --- | --- |
| 交互 | `BUSY_TIMEOUT_INTERACTIVE` | `WRK_BUSY_TIMEOUT_INTERACTIVE` | `3000` ms | 列知识库、看任务、登录审计：宁可快速失败，也不让界面卡住 |
| Worker | `BUSY_TIMEOUT_WORKER` | `WRK_BUSY_TIMEOUT_WORKER` | `30000` ms | 后台任务写：多等一会儿，避免把任务判失败 |
| 进度 | `BUSY_TIMEOUT_PROGRESS` | `WRK_BUSY_TIMEOUT_PROGRESS` | `1000` ms | `tasks.stage/progress` 的 best-effort 写，丢了只是少刷新一次 |

`config.py` 的注释说明旧版一刀切 15000ms 会让「任何一次外部持锁把 UI 卡满 15 秒」；
`WORKER_CONCURRENCY=1` 的原因是「SQLite 同时只允许一个写者，多开 worker 只会互相抢写锁」。
**`docs/DESIGN.md` 原先 §1.2 写 `15000` / `2`、§4.5 写 `3s`，两处已统一到本表口径。**

### 1.2 README §5 补齐的 11 个配置项（D6）

| 环境变量 | 默认 | 对应 `config.py` 符号 |
| --- | --- | --- |
| `WRK_BUSY_TIMEOUT_INTERACTIVE` | `3000` | `BUSY_TIMEOUT_INTERACTIVE` |
| `WRK_BUSY_TIMEOUT_WORKER` | `30000` | `BUSY_TIMEOUT_WORKER` |
| `WRK_BUSY_TIMEOUT_PROGRESS` | `1000` | `BUSY_TIMEOUT_PROGRESS` |
| `WRK_LOCK_RETRY_ATTEMPTS` | `5` | `LOCK_RETRY_ATTEMPTS` |
| `WRK_LOCK_RETRY` | `5`（兼容别名，被上一项覆盖） | `LOCK_RETRY_ATTEMPTS` 的回退 |
| `WRK_AGENT_ENABLED` | `1` | `AGENT_ENABLED` |
| `WRK_AGENT_PLUGIN_DIR` | `<DATA_DIR>/agent_plugins` | `AGENT_PLUGIN_DIR` |
| `WRK_AGENT_WORKSPACE` | `<DATA_DIR>/agent_workspace` | `AGENT_WORKSPACE_DIR` |
| `WRK_AGENT_RUN_TIMEOUT` | `900` | `AGENT_RUN_TIMEOUT_SECONDS` |
| `WRK_AGENT_HISTORY_MESSAGES` | `12` | `AGENT_HISTORY_MESSAGES` |
| `WRK_AGENT_HISTORY_CHARS` | `1200` | `AGENT_HISTORY_CHARS` |

另需更正 README §5 的一句总述：`WRK_HOST` / `WRK_PORT` / `WRK_WATCH_STDIN` / `WRK_LOG_LEVEL`
在 `backend/app/desktop_entry.py`，`WRK_PBKDF2_ITER` 在 `backend/app/security.py`，
`WRK_SUGGESTION_LIMIT` / `WRK_SUGGESTION_RESOURCE_LIMIT` 在 `backend/app/services/answering.py`，
`WRK_PACKAGED` 由 Electron 主进程注入——它们不在 `config.py` 里。

---

## 2. 测试基线（全仓唯一记录）

> **这里回答体检清单遗留的开放问题**：测试基线以哪次运行输出为准、写在哪里才不会
> 再散落多处？
>
> **答**：以**一次真实全量运行**为准；原始输出随仓库保存在
> `docs/evidence/pytest-baseline.txt`，数字**只写在本节这一处**；
> 其余六份文档一律改成指向本节的链接，不再自带数字。
> **CI（批次二 / P1-1，`.github/workflows/ci.yml`）已建立**，由它在每次 push / PR 上
> 自动产出权威运行输出，维护口径见 §2.4。

### 2.1 运行口径

| 项 | 值 |
| --- | --- |
| 命令 | `python -m pytest backend/tests -q` |
| 工作目录 | 仓库根目录 |
| 环境 | Windows 11（10.0.26200）、Python 3.13.11、pytest 9.2.0、本机全局解释器（当前 checkout 无 `.venv`） |
| 数据隔离 | `backend/tests/conftest.py` 用独立临时目录并关闭 Worker 与预热，不污染 `data/` |
| 本轮实跑 | 2026-11，`583 passed, 1 skipped in 207.46s (0:03:27)` |

### 2.2 一次真实运行输出（原文，完整内容见证据文件）

```
........................................................................ [ 12%]
........................................................................ [ 24%]
........................................................................ [ 36%]
........................................................................ [ 49%]
........................................................................ [ 61%]
...............................s........................................ [ 73%]
........................................................................ [ 86%]
........................................................................ [ 98%]
........                                                                 [100%]
583 passed, 1 skipped in 207.46s (0:03:27)
```

> 上面最后一行进度条按原样转录；`docs/evidence/pytest-baseline.txt` 是同一次运行
> 未经改写的完整 stdout（由 UTF-16 转存为 UTF-8）。**以证据文件为准。**

### 2.3 数字的来龙去脉

- **583 passed, 1 skipped**：`backend/tests/` 有 **42 个 `test_*.py` + 1 个 `conftest.py`**，
  文件内 **403 个 `def test_`**、**73 个 `@pytest.mark.parametrize`**；参数化展开后正好得到 583 项。
  这与体检清单 §3.1 的实测（583 passed / 170.56 s）相互印证。
- **1 skipped 的来源**：`backend/tests/test_tables.py` 中生成 PDF 表格夹具的用例调用了
  `pytest.importorskip("reportlab", reason="需要 reportlab 生成 PDF 夹具")`；本机未安装
  可选的 `reportlab`，该用例被跳过。用 `python -m pytest backend/tests -q -rs` 复核可见
  `SKIPPED [1] backend/tests/test_tables.py:319`。**它是可选依赖缺失，不是漏测，也不是新增 skip。**
- **耗时不是验收指标**：同机另一次 `-rs` 复核为 197.53 s、交付前最终复核为 114.57 s，
  体检时为 170.56 s，随机器负载变化；**passed / skipped 计数在多次运行中稳定为 583 / 1**。
- 旧 README 的 `535 passed` / `28 个文件，300 项` 是早期快照，已由本节取代；
  仓库内原先没有任何测试报告佐证 535。

### 2.4 维护约定（避免再次散落）

> 批次二 P1-1 的 CI 已落地，本节的「CI 建立后如何维护」从约定变成可执行流程。
> CI 首次运行前，§2.1 / §2.2 仍沿用批次一那次实跑作为初始基线；首次 CI 通过后按第 3 条同步。

1. **只有本节可以出现 passed/skipped 数字**。六份文档一律写「见 `docs/FACTS.md` §2」，
   不写数字、不写「约 580 项」这类近似值；README 顶部只放 CI 状态徽标。
2. **CI 是权威运行**：`.github/workflows/ci.yml` 在每次 push 到 `master` / PR 上，用
   `backend/requirements-dev.txt` + `backend/constraints.txt` 装出与本地一致的环境
   （Python 3.13），运行 `python -m pytest backend/tests -q`，并把完整 stdout 存为名为
   `pytest-baseline` 的 workflow artifact（保留 30 天）。
3. **更新基线**：从**最近一次 CI 通过**的 artifact 取数，覆盖
   `docs/evidence/pytest-baseline.txt`，并同步 §2.1 的「本轮实跑」与 §2.2 的转录。
   CI 未通过不得更新数字；本机为排障临时跑出的数字不写进本节。
   （CI 不回写仓库：自动写回需要带写权限的机器人提交，最小 CI 不引入该权限。）
4. **不得新增 skip**；若环境变化导致新增 skip，必须在本节写明是哪条用例、因何跳过。
5. 本节数字若与 CI artifact 不一致，以 CI artifact 为准，并在本节注明更正原因。

---

## 3. 代码锚点约定（对应 D7）

体检 §3.2 D7 与 §5.3 发现 `文件:行` 大面积漂移（`README.md` 抽查 8 处、4 处指错：
`config.py:27` 实为 `ALLOWED_SUFFIXES` 在 `config.py:49`；`main.py:27` 实为
`bootstrap_admin()` 在 `main.py:39`；`retrieval.py:264` 实为 `rrf_fuse()` 在 `:285`；
`build.mjs:53-57` 实为 `cleanEnv()` 内 `delete` 在 `:61`）。

**新约定**：引用代码一律写成「**文件 + 符号名 / 章节**」，行号最多作为写作时的辅助提示，
不得当作权威定位；代码是唯一事实源。常用对照：

| 想说的内容 | 写法 | 本次核对的当前位置（仅供抽查） |
| --- | --- | --- |
| 版本号 | `backend/app/main.py` 的 `VERSION` | `main.py:36` |
| 首次引导建号 | `backend/app/main.py` 的 `bootstrap_admin()` | `main.py:39` |
| 上传格式白名单 | `backend/app/config.py` 的 `ALLOWED_SUFFIXES` | `config.py:49` |
| 库内加权 RRF | `backend/app/services/retrieval.py` 的 `rrf_fuse()` / `channel_weights()` | `:285` / `:308` |
| 构建前清理环境变量 | `frontend/scripts/build.mjs` 的 `cleanEnv()` | `build.mjs:59`（`delete` 在 `:61`） |
| 文本 / 表格分块 | `backend/app/services/chunking.py` 的 `split_blocks()` / `split_table()` | `:158` / `:76` |
| Wiki 归纳入口 | `backend/app/services/wiki.py` 的 `build_wiki()` / `_plan_shards()` | `:461` / `:148` |

以上 6 处（外加 `models.py` 的 `__tablename__`）均可直接定位到所述内容，满足
「抽查 5 处 `文件:行` 均能定位」的验收要求。`docs/archive/HANDOFF_HISTORY.md` 里的
历史行号保留原样，但已在文件头声明为「当时的读数」。

---

## 4. 六份文档各自怎么引用本文件

| 文档 | 本波改动 |
| --- | --- |
| `README.md` | 版本、并发、锁超时、表数改为 §1 口径；§5 补齐 §1.2 的 11 项；测试基线改为 §2 链接；§1「代码位置」列改为 §3 的符号锚点 |
| `docs/DESIGN.md` | §1.2 与 §4.5 统一到 §1.1 的三档与单 Worker，消除内部矛盾；附录测试基线改为 §2 链接 |
| `docs/EXPERIMENT.md` | §1.1、§1.2 参数表、§5.1、§7 的并发/锁超时/测试基线改为 §1 / §2 口径；**§8 实验结果原样保留** |
| `docs/THESIS.md` | §0 版本与锚点约定、§1、§2.1/§2.2 的并发/锁超时/表数改为 §1 口径；§7 取证表改 §2 链接；**§4 实测结果原样保留** |
| `docs/HANDOFF.md` | 首屏重写为 v1.0.17 当前状态（版本、状态、已知遗留、如何验证），历史快照移入 `docs/archive/HANDOFF_HISTORY.md` |
| `docs/API.md` | §8 补齐 `discover` / `models/import` 契约行，新增 `/api/diagnostics/*` 契约行 |

---

## 5. 本波明确没做的事（避免误读）

- **不删除「待实验」标注**。第一波因本机无在线模型凭据，只跑出配置 A 离线结果；
  配置 B 在线、正确性人工标注、Cohen's κ 与检验的实际 p 值、真实 embedding 下的耗时
  仍标「待实验」。`docs/EXPERIMENT.md` §8.6、`docs/THESIS.md` §4.5、
  `docs/results/summary.md` 末节与 `docs/results/README.md` §8 的这些标注**不是文档错误**，
  按 `docs/EXPERIMENT.md:3`「凡未能实测的一律标注待实验」的仓库约定保留。
- **不覆盖第一波的实验结果**：`docs/results/` 16 个文件、`docs/EXPERIMENT.md` §8、
  `docs/THESIS.md` §4 原样保留。
- **本波（批次一）不建 CI、不锁依赖、不补前端测试**；当时只把「CI 建立前基线写在哪里」
  定下来。批次二之后 P1-1（CI）、P1-2（锁依赖）与 P1-3（前端自动化测试）均已落地：见
  `.github/workflows/ci.yml`、`backend/requirements*.txt`、`backend/constraints.txt`、
  `frontend/tests/` 与 `frontend/package.json` 的 `test` 脚本；§2.4 的维护约定已同步更新。
  CI 现在跑四项检查（后端 pytest、前端 `typecheck` / `build:web` / `test`），任意一项失败
  即阻断合并。前端 E2E 用确定性 mock 拦截全部 `/api/**`，离线可重复，
  详见 `frontend/tests/README.md`。
- **不改产品代码**（`backend/app/**`、`frontend/**`）；本波只动文档与新增证据/归档文件。
- 不清理写死的本机路径、`.dsh-kanban.json`、截图等 P2 项；这些仍在
  `docs/HEALTH_CHECK.md` §4.3 的清单里，可在答辩准备阶段并行处理。

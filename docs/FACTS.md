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
| 后端测试基线 | **638 passed, 1 skipped**（见 §2） | CI 步骤「全量后端测试」的 artifact `pytest-baseline`（证据文件 `docs/evidence/pytest-baseline.txt`） | 六份文档 8 个互不相同的数字 |

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
| 权威环境（CI） | `windows-latest`（Windows Server 2022）、Python 3.13.17、依赖来自 `backend/requirements-dev.txt` + `backend/constraints.txt` |
| 本机复核环境 | Windows 11（10.0.26200）、Python 3.13.11、pytest 9.2.0、全局解释器（当前 checkout 无 `.venv`） |
| 数据隔离 | `backend/tests/conftest.py` 用独立临时目录并关闭 Worker 与预热，不污染 `data/` |
| 当前基线 | **`638 passed, 1 skipped`** |
| CI 实测 | CI #14（run `42860672282`，job `129985607428`），`638 passed, 1 skipped, 1 warning in 196.41s (0:03:16)` |
| 本机复核 | 2026-11，同一提交 `638 passed, 1 skipped in 119.83s (0:01:59)` |

> **为什么 `638` 可信**：CI #14 这一次 run 整体是 failure，但失败的步骤是它**之后**的
> 「按 frozen lockfile 安装前端依赖」，与本节的计数无关；产出本基线的步骤
> 「检查 1/4｜全量后端测试」是 `completed/success`，artifact `pytest-baseline` 即其 stdout。
> 另有两处独立佐证：① CI #18（run `42866987077`，push `master@350009c`）13 步全绿，
> 其中后端测试步骤同样 success；② 在同一提交 `350009c` 上本机全量复跑，进度条逐行与
> CI 输出一致、计数同为 638/1（仅耗时与 warning 行不同）。

### 2.2 CI artifact 原文（完整内容见证据文件）

```
........................................................................ [ 11%]
........................................................................ [ 22%]
........................................................................ [ 33%]
........................................................................ [ 45%]
........................................................................ [ 56%]
........................................................................ [ 67%]
..........s............................................................. [ 78%]
........................................................................ [ 90%]
...............................................................          [100%]
638 passed, 1 skipped, 1 warning in 196.41s (0:03:16)
```

> 上面按 CI 步骤「检查 1/4｜全量后端测试」的 stdout 原样转录（该步骤用
> `Tee-Object` 落盘为 artifact `pytest-baseline`），`docs/evidence/pytest-baseline.txt`
> 是同一份内容。**以证据文件为准。**
> 本机复跑的最后一行是 `638 passed, 1 skipped in 119.83s (0:01:59)`：
> 少了 `1 warning`，因为该 warning 来自 CI 环境里 starlette/httpx 的版本组合（见 §2.3）。

### 2.3 数字的来龙去脉

- **638 passed, 1 skipped**：`backend/tests/` 有 **43 个 `test_*.py` + 1 个 `conftest.py`**，
  文件内 **430 个 `def test_`**、**77 个 `@pytest.mark.parametrize`**；参数化展开后正好得到 638 项。
  三条分解数字可用同一条命令复核：
  `Get-ChildItem backend/tests -Filter 'test_*.py' -Recurse`、
  `Select-String -Pattern '^\s*def test_'`、`Select-String -Pattern '@pytest\.mark\.parametrize'`。
- **1 skipped 的来源（与上一版相同，未新增）**：`backend/tests/test_tables.py:319` 调用
  `pytest.importorskip("reportlab", reason="需要 reportlab 生成 PDF 夹具")`；本机与 CI 都未安装
  可选的 `reportlab`，该用例被跳过。用 `python -m pytest backend/tests -q -rs` 复核可见
  `SKIPPED [1] backend/tests/test_tables.py:319`。**它是可选依赖缺失，不是漏测，也不是新增 skip。**
- **CI 的 `1 warning` 不是项目代码问题**：CI 日志里是
  `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead.`，
  出自第三方 `starlette` 的 `testclient.py`，由 CI 环境里 starlette/httpx 的版本组合触发；
  本机同一提交复跑没有该 warning。
- **耗时不是验收指标**：CI 196.41 s；本机同一提交两次复核分别为 119.83 s 与 196.70 s，
  随机器负载变化；**passed / skipped 计数在 CI 与本机均为 638 / 1**。
- 上一版写在这里的 `583 passed` / 42 个 `test_*.py` / 403 个 `def test_` / 73 个 `parametrize`
  是批次一（2026-11 早段）的快照，已随批次三新增的测试全部过时，本节统一按当前测试树重算。

### 2.4 维护约定（避免再次散落）

> 批次二 P1-1 的 CI 已落地，本节的「CI 建立后如何维护」是可执行流程；
> 批次三 P1-5 又给 CI 加了第 5 项检查（契约一致性），不影响本节口径。

1. **只有本节可以出现 passed/skipped 数字**。六份文档一律写「见 `docs/FACTS.md` §2」，
   不写数字、不写「约 630 项」这类近似值；README 顶部只放 CI 状态徽标。
2. **CI 是权威运行**：`.github/workflows/ci.yml` 在每次 push 到 `master` / PR 上，用
   `backend/requirements-dev.txt` + `backend/constraints.txt` 装出与本地一致的环境
   （Python 3.13），运行 `python -m pytest backend/tests -q`，并把完整 stdout 存为名为
   `pytest-baseline` 的 workflow artifact（保留 30 天）。
3. **更新基线**：从**最近一次 CI 通过的后端测试步骤**的 artifact 取数，覆盖
   `docs/evidence/pytest-baseline.txt`，并同步 §2.1 的「CI 实测」与 §2.2 的转录。
   该步骤未通过不得更新数字；若整轮 run 因**其它步骤**失败，须在 §2.1 写明是哪一步、
   与本计数无关，并给出至少一处独立佐证。本机为排障临时跑出的数字只作为佐证，不单独作为基线。
   （CI 不回写仓库：自动写回需要带写权限的机器人提交，最小 CI 不引入该权限。）
4. **不得新增 skip**；若环境变化导致新增 skip，必须在本节写明是哪条用例、因何跳过。
5. 本节数字若与 CI artifact 不一致，以 CI artifact 为准，并在本节注明更正原因。
6. **改基线时分解数字必须一起重算**：文件数 / `def test_` / `parametrize` 三项与 passed 数
   是同一套事实的四张脸，只改标题数字会立刻自相矛盾。

### 2.5 本轮已知残留（如实记录，未在本任务范围内修改）

- `backend/requirements.txt` 第 5 行的注释仍写 `583 passed, 1 skipped`。本任务的文件所有权
  限定为 `frontend/**`、`docs/API.md`、`docs/FACTS.md` 与 CI 步骤（并行任务正在拆
  `backend/app/services/llm.py` / `routers/kbs.py`），因此**未改动该文件**；
  按 §2.4 第 1 条，该注释应当在下一个碰 `backend/**` 的任务里改成指向本节。
- `docs/HEALTH_CHECK.md` §3.1 的 `583` 是体检当时的实测快照，属于历史记录，**不回改**。


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
  CI 现在跑**五项**检查（后端 pytest、前后端契约 `check:contract`、前端 `typecheck` /
  `build:web` / `test`），任意一项失败即阻断合并。第 2 项由批次三 P1-5 加入，
  见 `frontend/scripts/check-api-contract.mjs` 与 `docs/API.md` §10。
  前端 E2E 用确定性 mock 拦截全部 `/api/**`，离线可重复，详见 `frontend/tests/README.md`。
- **不改产品代码**（`backend/app/**`、`frontend/**`）；本波只动文档与新增证据/归档文件。
- 不清理写死的本机路径、`.dsh-kanban.json`、截图等 P2 项；这些仍在
  `docs/HEALTH_CHECK.md` §4.3 的清单里，可在答辩准备阶段并行处理。

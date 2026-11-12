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
| 应用版本 | `1.0.22` | `backend/app/main.py` 的 `VERSION`；`frontend/package.json` 的 `version` | THESIS.md `1.0.0`、HANDOFF.md 首屏 `v1.0.10` |
| 数据表数量 | **24 张**：`models.py` 声明 23 张 + `schema_migrations` 1 张 | `backend/app/models.py` 的 23 个 `__tablename__`；`backend/app/migrations.py` 的 `_CREATE_TABLE`（原始 SQL 建表，**不进** `Base.metadata`，所以 `create_all()` 不建它）。数法与输出见 `docs/evidence/table-count-2026-11-11.txt` | THESIS.md 19 张；本表旧口径 21 张（`models.py` 实际是 22 张，漏算 1 张，见下方注） |
| Worker 并发 | **1** | `backend/app/config.py` 的 `WORKER_CONCURRENCY`（`WRK_WORKER_CONCURRENCY` 默认 `"1"`） | `2`（5 份文档） |
| SQLite 写锁等待 | **三档 3s / 30s / 1s**（见 §1.1） | `backend/app/config.py` 的 `BUSY_TIMEOUT_INTERACTIVE` / `BUSY_TIMEOUT_WORKER` / `BUSY_TIMEOUT_PROGRESS` | 一刀切 `busy_timeout=15000`；DESIGN.md §4.5 又写 `3s` |
| 拿不到锁的重试次数 | `5`（`WRK_LOCK_RETRY_ATTEMPTS`，兼容别名 `WRK_LOCK_RETRY`） | `backend/app/config.py` 的 `LOCK_RETRY_ATTEMPTS` | 未进文档 |
| 可调项 | `config.py` 中 **43 个 `WRK_*`** 环境变量（另有若干由 `desktop_entry.py` / `security.py` / `answering.py` 读取） | `backend/app/config.py`；数法与复现命令见 §1.3 | 本表旧口径 38 个；批次三（P1-8）把它写成 41（算式给的是 38 + 5 = 43，属笔误，见下方注）；README §5 漏列 11 个（见 §1.2） |
| 后端测试基线 | **784 passed, 1 skipped**（见 §2） | 本机实跑，原始输出 `docs/evidence/pytest-baseline.txt`；CI 状态见 §2.1 | 六份文档 8 个互不相同的数字 |

> 表里「当前值」一列在批次三（P1-8）逐条回查代码确认，批次五（文档口径收尾）又按代码重数了
> 可调项条数；D1–D7 的结论在代码里**仍然成立**（`backend/app/config.py` 的
> `WORKER_CONCURRENCY` 仍是 `1`、`BUSY_TIMEOUT_*` 仍是三档，`backend/app/main.py` 的
> `VERSION` 已随发版递增到 `1.0.22`，与 §1 表内「应用版本」一致）。
> 两处旧口径按代码订正：`models.py` 实际是 **22** 个 `__tablename__`（旧写 21，少算 1 张），
> 加上 P1-8 新增的 `schema_migrations` 共 **23** 张；P1-11 又新增 `model_prices` 1 张，
> 现为 **24 张**（§1 表内已更新，重数命令与输出见
> `docs/evidence/table-count-2026-11-11.txt`）；`config.py` 实际是 **43** 个 `WRK_*`
> （旧写 38；P1-8 新增 4 个 `WRK_LOGIN_*` 与 `WRK_HARDEN_SECRETS`，38 + 5 = 43，清单见 §1.2）。
> **批次三写的 41 是算式笔误、不是后来又漏了两项**：它在同一段里写的是「38 + 5」，
> 结果却落成 41，与 §1.2 的清单和代码都对不上；批次五用 §1.3 的命令重数得到 **43**，
> 命令与输出记在 `docs/evidence/wrk-count-2026-11-11.txt`。其余文档从此不再复述这个条数。
> `README.md` 目录树注释与 `docs/THESIS.md` §2.1 / §2.2 原本写着「21 张表」，已在批次四
> （P2 仓库治理）按本节订正为 **23 张表**（`models.py` 的 22 个 + `migrations.py` 的
> `schema_migrations`）；P1-11 新增 `model_prices` 后这三处同步改成 **24 张表**
> （`models.py` 的 23 个 + `schema_migrations`），`THESIS.md` §2.2 的分组表也补上了
> `model_prices` 与原本漏列的 `agent_sessions` / `agent_messages` /
> `wiki_generation_sources`；`THESIS.md` §4 是实验结果，按仓库约定不动。

### 1.1 SQLite 写锁三档（替代旧的一刀切 `15000ms`）

| 档 | 变量 | 环境变量 | 默认 | 用途 |
| --- | --- | --- | --- | --- |
| 交互 | `BUSY_TIMEOUT_INTERACTIVE` | `WRK_BUSY_TIMEOUT_INTERACTIVE` | `3000` ms | 列知识库、看任务、登录审计：宁可快速失败，也不让界面卡住 |
| Worker | `BUSY_TIMEOUT_WORKER` | `WRK_BUSY_TIMEOUT_WORKER` | `30000` ms | 后台任务写：多等一会儿，避免把任务判失败 |
| 进度 | `BUSY_TIMEOUT_PROGRESS` | `WRK_BUSY_TIMEOUT_PROGRESS` | `1000` ms | `tasks.stage/progress` 的 best-effort 写，丢了只是少刷新一次 |

`config.py` 的注释说明旧版一刀切 15000ms 会让「任何一次外部持锁把 UI 卡满 15 秒」；
`WORKER_CONCURRENCY=1` 的原因是「SQLite 同时只允许一个写者，多开 worker 只会互相抢写锁」。
**`docs/DESIGN.md` 原先 §1.2 写 `15000` / `2`、§4.5 写 `3s`，两处已统一到本表口径。**

### 1.2 环境变量清单（D6 补齐的 11 项 + P1-8 新增 5 项）

本节只列**当时补齐与新增**的部分，不是 `config.py` 的全部 `WRK_*`——43 项的完整清单与数法见 §1.3。

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

**P1-8 新增（登录限流与密钥权限；尚未进 README §5，归下一个碰 README 的任务）**

| 环境变量 | 默认 | 对应 `config.py` 符号 |
| --- | --- | --- |
| `WRK_LOGIN_MAX_FAILURES` | `5` | `LOGIN_MAX_FAILURES` |
| `WRK_LOGIN_MAX_FAILURES_PER_SOURCE` | `20` | `LOGIN_MAX_FAILURES_PER_SOURCE` |
| `WRK_LOGIN_FAIL_WINDOW` | `900` | `LOGIN_FAIL_WINDOW_SECONDS` |
| `WRK_LOGIN_LOCK_SECONDS` | `900` | `LOGIN_LOCK_SECONDS` |
| `WRK_HARDEN_SECRETS` | `1` | `HARDEN_SECRET_PERMISSIONS` |

四个 `WRK_LOGIN_*` 的语义：**账号**维度 5 次、**来源**维度 20 次，在 900 秒窗口内达到阈值即锁定
900 秒；锁定期间即使密码正确也返回 429（联调时别把它当登录失败排查，响应头带 `Retry-After`）。
`WRK_HARDEN_SECRETS=0` 关掉密钥文件/目录的权限收紧（POSIX 0600/0700、Windows `icacls`），
关掉不影响密钥本身可用，只是不再改权限。这 5 项都在 `backend/app/config.py`，与上表 11 项同源，
因此计入 §1「可调项 **43 个**」（数法与复现命令见 §1.3）。

另需更正 README §5 的一句总述：`WRK_HOST` / `WRK_PORT` / `WRK_WATCH_STDIN` / `WRK_LOG_LEVEL`
在 `backend/app/desktop_entry.py`，`WRK_PBKDF2_ITER` 在 `backend/app/security.py`，
`WRK_SUGGESTION_LIMIT` / `WRK_SUGGESTION_RESOURCE_LIMIT` 在 `backend/app/services/answering.py`，
`WRK_PACKAGED` 由 Electron 主进程注入——它们不在 `config.py` 里。

### 1.3 可调项条数的数法（可复现）

条数不靠人工点、也不信任何一份文档的现成数字，用下面这条命令数。仓库根目录执行
（Python 3.13，无第三方依赖）：

```bash
python -c "import re,pathlib;print(len(set(re.findall(r'\bWRK_[A-Z0-9_]+\b', pathlib.Path('backend/app/config.py').read_text(encoding='utf-8')))))"
```

输出 **`43`**。两个口径交叉校验一致：只数 `os.getenv("WRK_...")` 的实参同样得 `43`，
且两个集合完全重合——43 个名字**全部**真的被 `os.getenv` 读取，没有「只在注释里出现」的幽灵项。
其中 `WRK_LOCK_RETRY` 是 `WRK_LOCK_RETRY_ATTEMPTS` 的兼容别名（同一处 `os.getenv` 的外层与内层，
见 `config.py` 的 `LOCK_RETRY_ATTEMPTS`），按「一个环境变量名算一项」计入。

**口径边界**：只数 `backend/app/config.py` 里的 `WRK_*`。`desktop_entry.py` / `security.py` /
`answering.py` 读的那 8 个（上一段列出的 `WRK_HOST`、`WRK_PORT`、`WRK_WATCH_STDIN`、
`WRK_LOG_LEVEL`、`WRK_PBKDF2_ITER`、`WRK_SUGGESTION_LIMIT`、`WRK_SUGGESTION_RESOURCE_LIMIT`、
`WRK_PACKAGED`）**不计入** 43——它们在别的文件里，改动节奏与 `config.py` 不同。
命令、输出、43 个名字的完整清单与两种数法的对照，都记在
`docs/evidence/wrk-count-2026-11-11.txt`。

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
| 权威环境（CI） | `windows-latest`、Python 3.13.x、依赖来自 `backend/requirements-dev.txt` + `backend/constraints.txt` |
| 本次实测环境 | Windows 11（10.0.26200）、Python 3.13.11、pytest 9.2.0、仓库根 `.venv`（按 CONTRIBUTING 用 `https://pypi.org/simple` 重建，依赖同一套锁定文件；v1.0.19 发版门禁也在这个 `.venv` 上整跑，见 `docs/evidence/release-v1.0.19-build.txt` §3） |
| 数据隔离 | `backend/tests/conftest.py` 用独立临时目录并关闭 Worker 与预热，不污染 `data/` |
| 当前基线 | **`784 passed, 1 skipped`** |
| 本次实测 | 「个人 Agent 把思考过程与最终回答分开显示」的本分支工作区（基于已含 v1.0.19 的 `master`），`784 passed, 1 skipped in 143.63s (0:02:23)`（同树另一次实跑 `222.38s`，计数相同） |
| 独立佐证 | 上一版 `775` 由 `541` 个 `def test_` / `56` 个测试文件 / `83` 个 `parametrize` 展开成 `776` 项；本版 `548` / `56` / `83`：新增 7 条 `def test_`（`test_agent_runtime.py` 5 条推理/回答分区用例、`test_agent_plugins.py` 2 条清单校验），并在 `test_parse_manifest_rejects_bad_fields` 的参数表加 2 行（`answer_item_types` 的两种错法）。`+7 +2 = +9`，`775 + 9 = 784` 与 §2.3 自洽，可用该节同一套命令复核 |
| CI 状态 | **待验（本分支尚未跑 CI）**：本轮改动 `backend/**`、`frontend/src/**`、`frontend/tests/**`、`docs/**`，五项门禁本机全绿；按 §2.4 第 3 条的例外处理：先记本机实跑，CI artifact 出来后覆盖本行与 §2.2。更早的 CI #33（run `42947552959`，基线 `707` 那棵树）仍是最近一次真跑过 CI 的记录 |

> **为什么这里记的是本机数字，而不是 CI artifact**：§2.4 第 3 条要求从 CI artifact 取数，
> 但本轮改动还没有任何 CI 运行（分支工作区，合并 `origin/master` 后才会触发）。
> 按「拿不到远端结果就写待验、并给出本机等价证据」的口径，先落本机实跑并把 CI 标为待验。
> **历史**：上一版基线 `707` 的树已随 PR #58 并入 `master` 并在 CI #33 全绿，
> 但本机取不到它的 artifact；`754` 那一版同样是本机实跑、CI 待验。
>
> 本机与 CI 在同一提交上的历史一致性可以支撑「本机等价证据」这个说法：更早的基线（`638`）时，
> CI #14（run `42860672282`，job `129985607428`）的 artifact 与本机在同一提交 `350009c` 上的复跑
> **进度条逐行相同、计数完全相同**，只差耗时与 CI 环境特有的 warning 行。但它不是同一份 artifact，
> 所以本节的来源标注不写成「CI 实测」。

### 2.2 一次真实运行输出（原文，完整内容见证据文件）

```
........................................................................ [  9%]
........................................................................ [ 18%]
........................................................................ [ 27%]
........................................................................ [ 36%]
........................................................................ [ 45%]
........................................................................ [ 55%]
........................................................................ [ 64%]
....................................s................................... [ 73%]
........................................................................ [ 82%]
........................................................................ [ 91%]
.................................................................        [100%]
784 passed, 1 skipped in 143.63s (0:02:23)
```

> 上面是本次实跑的 stdout 原样转录（`pytest -q`，`Out-File -Encoding utf8` 落盘后转成无 BOM 的 UTF-8），
> `docs/evidence/pytest-baseline.txt` 是同一份内容，共 12 行。**以证据文件为准。**
> 本机这次实跑另有 1 条 `StarletteDeprecationWarning`，出现在 pytest 单独汇总的 warnings
> summary 段（来自 `.venv` 里 httpx/starlette 的版本组合，见 §2.3），不是用例失败；
> 最后一行没有 `N warning`，是因为 `-q` 把警告单列成段，不并进计数行。

### 2.3 数字的来龙去脉

- **784 passed, 1 skipped**：`backend/tests/` 有 **56 个 `test_*.py` + 1 个 `conftest.py`**
  （共 57 个 `.py`），文件内 **548 个 `def test_`**、**83 个 `@pytest.mark.parametrize`**；
  参数化展开后正好得到 785 项（784 passed + 1 skipped）。三条分解数字可用同一条命令复核：
  `Get-ChildItem backend/tests -Filter 'test_*.py' -Recurse`、
  `Select-String -Pattern '^\s*def test_'`、`Select-String -Pattern '@pytest\.mark\.parametrize'`。
- **比上一版（775）多 9 项的原因**：本轮「个人 Agent 把思考过程与最终回答分开显示」
  在 `test_agent_runtime.py` 新增 5 条（推理不进正文、命令项算过程、旧清单兼容、
  只有推理时报「没有最终回答」、思考过程不回放进下一轮提示词），在 `test_agent_plugins.py`
  新增 2 条（清单接受 `answer_item_types`、内置 Codex 声明它）；`test_parse_manifest_rejects_bad_fields`
  参数表再加 2 行（纯文本输出声明该字段、字段不是数组）。对应 `541 → 548` 个 `def test_`、
  `parametrize` 装饰器仍为 **83**、`775 + 7 + 2 = 784`。**没有新增失败用例，也没有新增 skip。**
- **775 的来历（上一版，保留作追溯）**：本轮任务「个人 Agent 改用应用独立 Codex 配置」新增
  `test_agent_codex_home.py`（15 条：应用独占目录、技能只读链接、`build_env` 注入 `CODEX_HOME`、
  用户 `~/.codex` 不被改写），并在既有 `test_agent_plugins.py` / `test_agent_runtime.py` 上做净
  +5 条增补——删掉随知识库核心下线的 `test_kb_session_flow`，补上历史 kb 会话的三条兼容用例
  （忽略 `kb_ids`、`core_id=kb` 的 400 提示、历史会话可读可换核心）与两条提示词用例
  （单行交付、直接回答）。对应 `55 → 56` 个测试文件、`521 → 541` 个 `def test_`；
  `parametrize` 装饰器仍为 **83**，但 `test_parse_manifest_rejects_bad_fields` 的参数表多了一行
  （`isolated_home`），所以展开项比 `def test_` 多涨 1 项，`754 → 775`。**没有新增失败用例，也没有新增 skip。**
- **754 的来历（上一版，保留作追溯）**：P1-11（个人 Agent 用量入统计 + 模型参考价）新增
  2 个测试文件（`test_model_prices.py`、`test_pricing_api.py`）并在 4 个既有文件里补用例：
  新增 **25 个 `def test_`**（`496 → 521`）、**3 个 `parametrize`**（`80 → 83`）。
  展开后正好 `+47`：`test_model_prices` 9 个函数（含 1 个 7 行参数化）→ 15 项；
  `test_pricing_api` 7 个函数（含 1 个 4 行参数化）→ 10 项；
  `test_agent_runtime.py` +3 个函数（`test_jsonl_usage_parser` 参数化 11 行）→ +13 项；
  `test_agent_plugins.py` +2 个函数、既有参数化表 +3 行 → +5 项；
  `test_usage.py` +3 项；`test_login_rate_limit.py` +1 项。合计 15+10+13+5+3+1 = 47。
- **707 的来历（上一版，保留作追溯）**：批次三 P1-8 新增 6 个测试文件，**全是新增用例，没有删改既有用例**：
  `test_must_change_password.py`（4）、`test_login_rate_limit.py`（6）、`test_upload_magic.py`（5）、
  `test_secret_permissions.py`（7）、`test_watchdog_logging.py`（5）、`test_schema_migrations.py`（7），
  合计 34。对应 `47 → 53` 个测试文件、`462 → 496` 个 `def test_`；`parametrize` 仍为 **80**
  （新用例都不带参数化），展开后 `673 → 707`。**这不是新增了失败用例，全部为 passed。**
- **历史（上一版 673 的来历，保留作追溯）**：`master` 的 Wiki 生成改动带进来 4 个新测试文件
  （`test_wiki_generation_source_migration.py`、`test_wiki_generation_visibility.py`、
  `test_wiki_input_source_race.py`、`test_wiki_input_timestamp.py`）以及既有文件的增补；
  `43 → 47` 个测试文件、`430 → 462` 个 `def test_`、`77 → 80` 个 `parametrize`，
  展开后 `638 → 673`。
- **1 skipped 的来源（与上一版相同，未新增）**：`backend/tests/test_tables.py:319` 调用
  `pytest.importorskip("reportlab", reason="需要 reportlab 生成 PDF 夹具")`；本机与 CI 都未安装
  可选的 `reportlab`，该用例被跳过。用 `python -m pytest backend/tests -q -rs` 复核可见
  `SKIPPED [1] backend/tests/test_tables.py:319`。**它是可选依赖缺失，不是漏测，也不是新增 skip。**
- **耗时不是验收指标**：本机绿跑的耗时随机器负载变化（本轮全量 `143.63 s`，同树另一次
  `222.38 s`；上一版 `775` 时是 `208.54 s`，`754` 时是 `212.82 s` / `231.24 s`）；
  **passed / skipped 计数稳定为 784 / 1**。负载高时另出现过与本次改动无关的偶发
  （`test_agent_runtime` / `test_freeze_during_task`，单独复跑均通过）；其中
  `test_agent_runtime` 那次的根因（用例向 `example.test` 发真实模型请求、被 DNS 拖满 15 秒）
  已随合并 `origin/master` 的 `offline_llm` 夹具修掉，见 §2.5。
- **warning 行与代码无关**：旧版 CI 曾出现 1 个
  `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead.`，
  出自第三方 `starlette` 的 `testclient.py`。本轮用重建的仓库根 `.venv` 实跑也出现同一条
  （httpx/starlette 版本组合触发），由 pytest 汇总在 warnings summary 段；它不是用例失败，
  也不改变 passed / skipped 计数。
- 更早的口径（`583`、`638`、`673`、`707`、`754`）都已过时：`583` 是批次一体检时的快照，`638` 是
  `master@350009c` 的基线，`673` 是 P1-8 合入 `origin/master` 之前的基线，`707` 是 P1-8 定稿
  基线，`754` 是 P1-11 定稿基线；本节按当前测试树统一重算。

### 2.4 维护约定（避免再次散落）

> 批次二 P1-1 的 CI 已落地，本节的「CI 建立后如何维护」是可执行流程；
> 批次三 P1-5 又给 CI 加了第 5 项检查（契约一致性），不影响本节口径。

1. **只有本节可以出现 passed/skipped 数字**。六份文档一律写「见 `docs/FACTS.md` §2」，
   不写数字、不写「大概六百多项」这类近似值；README 顶部只放 CI 状态徽标。
2. **CI 是权威运行**：`.github/workflows/ci.yml` 在每次 push 到 `master` / PR 上，用
   `backend/requirements-dev.txt` + `backend/constraints.txt` 装出与本地一致的环境
   （Python 3.13），运行 `python -m pytest backend/tests -q`，并把完整 stdout 存为名为
   `pytest-baseline` 的 workflow artifact（保留 30 天）。
3. **更新基线**：从**最近一次 CI 通过的后端测试步骤**的 artifact 取数，覆盖
   `docs/evidence/pytest-baseline.txt`，并同步 §2.1 的「CI 实测」与 §2.2 的转录。
   该步骤未通过不得更新数字；若整轮 run 因**其它步骤**失败，须在 §2.1 写明是哪一步、
   与本计数无关，并给出至少一处独立佐证。
   **唯一例外**（本节当前正处在这个例外里）：基线要描述的是刚合并、还没跑过 CI 的树，
   此时允许先写本机实跑，但必须同时满足三条——① 把 CI 明确标为「待验」；
   ② 给出本机等价证据与环境；③ CI artifact 出来后立即覆盖。
   （CI 不回写仓库：自动写回需要带写权限的机器人提交，最小 CI 不引入该权限。）
4. **不得新增 skip**；若环境变化导致新增 skip，必须在本节写明是哪条用例、因何跳过。
5. 本节数字若与 CI artifact 不一致，以 CI artifact 为准，并在本节注明更正原因。
6. **改基线时分解数字必须一起重算**：文件数 / `def test_` / `parametrize` 三项与 passed 数
   是同一套事实的四张脸，只改标题数字会立刻自相矛盾。

### 2.5 本轮已知残留（如实记录）

- ~~`backend/requirements.txt` 第 5 行的注释仍写 `583 passed, 1 skipped`~~ **本轮（P1-8）已修**：
  该行已改成指向本节、不再手写数字（P1-8 是第一批改动 `backend/**` 的任务）。
- `docs/HEALTH_CHECK.md` §3.1 的 `583` 是体检当时的实测快照，属于历史记录，**不回改**。
- `docs/PR42_FIX_REPORT.md` 的验收表里另有一份 `673 passed, 1 skipped`，并仍写着「四项检查」
  ——那是该任务交付当时的点对点报告（当时 CI 还是四项，P1-5 才加到五项）。它属于历史报告，
  本节不回改。
- ~~**与 P1-8 / P1-11 无关的已知偶发**：`backend/tests/test_agent_runtime.py` 的 `_wait_run()`
  只等 15 秒，全量跑在负载高时会偶发超时~~ **已修（2026-11-11，独立任务「让 test_agent_runtime
  的两条用例不再依赖真实 DNS」）**：根因不是「等得不够久」，而是这两条用例会拿假域名
  `https://example.test` 发一次真实模型请求——本机解析该域名要 11 秒，一次出网就吃掉 15 秒预算。
  改法是在测试侧断掉出网（`offline_llm` 夹具：`llm._client` 就地失败 + 非回环域名解析直接报错），
  **`_wait_run()` 的超时没有放宽**；改后 `707 passed, 1 skipped` 不变，`.test` 不可解析 / 解析拖成
  11 秒 / 断网三种条件下各连续 5 次全绿。根因、对照与逐次输出见
  `docs/evidence/agent-runtime-offline.txt`。
  P1-11 的全量跑在该修复进入本分支**之前**也命中过一次同根因的偶发
  （`test_kb_session_flow`，知识库核心同样会向 `example.test` 发模型请求）：单文件复跑
  `26 passed`、全量复跑 `754 passed`；合并本修复后不再依赖真实 DNS。
- ~~`Retry-After` 偶发多报 1 秒~~ **P1-11 已修**：`backend/app/services/login_guard.py` 的
  `now + lock_seconds - now` 会带 1e-10 级浮点尾巴，`ceil` 后 `Retry-After` 变成 `901`
  （本机 20 万次 `time.monotonic()` 采样中约 25% 命中），使
  `test_login_rate_limit.py` 的全量跑时好时坏。已改为 `_retry_after()`（先减 1 微秒 epsilon
  再 `ceil`）并补 `test_retry_after_rounds_up_without_float_noise` 锁定行为；
  输出见 `docs/evidence/pricing-gates-2026-11-11.txt` §四。



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

---

## 6. 旧数字扫查（2026-11-11 收尾）

批次五（文档口径收尾）在核准可调项条数的同时，把**已被取代的旧口径**在非归档文档里扫了一遍。
判据两条：**当前事实**必须与本文件一致；**历史语境下的正确值**（当时的实测快照、当时的交付报告、
归档证据）保持原样，只在本节登记。扫查命令、逐条命中与判断依据见
`docs/evidence/wrk-count-2026-11-11.txt` §4。

### 6.1 本轮已订正

| 位置 | 旧值 | 现值 | 说明 |
| --- | --- | --- | --- |
| 本文件 §1「可调项」、§1.2 末段 | `41 个 WRK_*` | **43 个** | `38 + 5 = 43`；41 是批次三的算式笔误，§1.3 的命令可复现 |
| `README.md` 目录树里测试目录的注释 | `53 个 test_*.py + 1 个 conftest.py` | 改为指向 §2，不复述条数 | 文件数属于 §2 的分解数字，按 §2.4 第 1、6 条只该出现在 §2 |

### 6.2 扫到的旧数字，按「历史语境」保留

| 位置 | 旧数字 | 为什么不动 |
| --- | --- | --- |
| `docs/HEALTH_CHECK.md` §2、§3.1、§3.2（D4 / D6）、§4.1（P0-3）、§5.3 | `583 passed`、`21 张表` / `21 个 __tablename__`、`38 个 WRK_*`、`42 个测试文件` | 体检当时（P0 批次）的实测与读数；§2.5 已声明属历史记录、不回改。它的价值恰恰是「当时是什么样」 |
| `docs/PR42_FIX_REPORT.md` §三「逐项验证结果」及其后的合并复核 | `618 / 673 passed`、「四项检查」 | 该任务的点对点交付报告，记的是它自己那一轮的验收；§2.1 已把它当作上一版基线的独立佐证引用 |
| `docs/archive/**`（含 `HANDOFF_HISTORY.md`） | `59 / 82 / 89 / 93 / 157 / 171 / 192 / 200 passed`、`WORKER_CONCURRENCY = 2`、`busy_timeout=15000` | 归档即历史证据；`docs/archive/README.md` 与 `HANDOFF_HISTORY.md` 文件头已声明「当时读数，不是当前值」 |
| 本文件 §0、§2.3、§2.5 | `21 张`、`583`、`638`、`673` | 它们是「旧口径是什么、怎么被取代」的追溯记录；§2 正是全仓唯一允许出现基线数字的地方（§2.4 第 1 条） |

### 6.3 复核结论

- 除本文件自身与上表的「历史语境」外，仓库里**不再有**把 `583` / `638` / `673` 当作当前基线的表述，
  也没有 `21 张表` / `38 个 WRK_` / `41 个 WRK_` 的现行口径。
- `README.md`、`docs/DESIGN.md`、`docs/EXPERIMENT.md`、`docs/THESIS.md`、`docs/HANDOFF.md`、
  `docs/API.md` 六份文档对版本、表数、并发、锁超时、测试基线一律只放链接，未复述数字；
  `CHANGELOG.md` 与 `CONTRIBUTING.md` 同样只写指向（两者已复核为真）。
- 本轮**只改文档**：`backend/**`、`frontend/**`、`.github/**` 零改动，`docs/archive/**` 零改动。

### 6.4 P1-11 追加订正（2026-11-11，个人 Agent 用量入统计 + 模型参考价）

P1-11 改动 `backend/**`，同时让两个口径发生变化：数据表 `23 → 24`（新增 `model_prices`）、
测试基线 `707 → 754`（新增用例）。按「当前事实必须与本节一致、历史语境保持原样」的判据，
本轮扫到并订正了下面几处**复述数字的位置**：

| 位置 | 旧值 | 现值 | 说明 |
| --- | --- | --- | --- |
| 本文件 §1「数据表数量」、§1 表后注、§6 各段引用 | `23 张`（`models.py` 22 张） | **24 张**（`models.py` 23 张） | 重数命令与输出见 `docs/evidence/table-count-2026-11-11.txt` |
| `README.md` 目录树的 `models.py` 注释 | `23 张表（22 个 __tablename__…）` | `24 张表（23 个 __tablename__…）` | 该行一直在写具体条数，属于要跟着本节同步的位置 |
| `docs/THESIS.md` §2.1 架构图、§2.2 数据模型首段 | `23 张表` / `22 个 __tablename__` | `24 张表` / `23 个 __tablename__` | 同时补上 §2.2 分组表漏列的 `agent_sessions`、`agent_messages`、`wiki_generation_sources` 与新表 `model_prices`（补完 5 组共 23 张，先加 `schema_migrations` = 24） |
| `docs/API.md` §5（schema_migrations 一节） | `「23 张表」= models.py 的 22 张 + 本表` | `「24 张表」= models.py 的 23 张 + 本表` | 同一口径 |
| 本文件 §2.1 / §2.2 / §2.3 | `707 passed, 1 skipped` | **`754 passed, 1 skipped`** | 分解数字按 §2.4 第 6 条一起重算 |

**历史语境仍未回改**：`docs/HEALTH_CHECK.md`（`21 张表` / `583`）、`docs/PR42_FIX_REPORT.md`
（`673` / 「四项检查」）、`docs/archive/**`，以及本文件里明确标为「上一版 / 更早口径」的
追溯段落。`docs/RELEASE_NOTES_v1.0.18.md` 记的是 v1.0.18 发版当时的内容，不复改。

### 6.5 个人 Agent 改用应用独立 Codex 配置（2026-11-11）追加订正

本任务把 Codex 核心切到应用自己的配置目录、下线知识库核心并改提示词，改动
`backend/**`、`docs/**` 与 `frontend/src/types.ts`；合并 `origin/master`（P1-11）后在本分支
工作区实跑，测试基线 `754 → 775`（新增用例明细见 §2.3）。按「当前事实必须与本节一致、
历史语境保持原样」的判据，本次只订正了本文件 §1「后端测试基线」一行与 §2.1 / §2.2 / §2.3
的基线数字（分解数字按 §2.4 第 6 条一起重算），并把 `docs/evidence/pytest-baseline.txt`
换成这次实跑的原文。数据表数量、`WRK_*` 条数等其它口径本轮未变，§6.4 的
`707 → 754` 与 `23 → 24 张表` 记录保持原样，属历史语境。

### 6.6 v1.0.19 发版（2026-11-11）追加订正

本轮只做发版：版本号 `1.0.18 → 1.0.19`，本文件只订正 §1「应用版本」一行与紧随其后的注记
（`VERSION` 递增到 `1.0.19`）。**其余口径一律未变**——数据表仍是 **24 张**、`WRK_*` 仍是 **43 个**、
后端测试基线仍是 **775 passed, 1 skipped**（§2），因此 §2 的分解数字与本节的 §6.4 / §6.5
都不需要重算。`docs/RELEASE_NOTES_v1.0.18.md`、`docs/evidence/release-v1.0.18-*.txt` 与
`docs/archive/**` 属历史记录，不回改；本版新增的交付说明与构建记录分别是
`docs/RELEASE_NOTES_v1.0.19.md` 与 `docs/evidence/release-v1.0.19-build.txt`。

发版门禁在本树实跑过一遍五项检查，后端仍是 **`775 passed, 1 skipped`**（`265.29s`，与本节的
记录计数完全相同，只是耗时不同），因此 §2.1、§2.2 记的那次基线运行**原样保留**、未按本轮重写。

### 6.7 个人 Agent 思考过程与回答分区（2026-11-11）追加订正

本任务修的是用户截图中「整段模型推理铺在回答气泡里、最终答案反而没出现」：后端把
JSONL 事件按 `item.type` 分成「回答」与「思考过程」两条通道，前端把思考过程收进默认
折叠的卡片。改动 `backend/**`、`frontend/src/**`、`frontend/tests/**`、`docs/**`；
本机五项门禁全绿，后端测试基线 `775 → 784`（+7 条用例、+2 行参数表，明细见 §2.3）。

按「当前事实必须与本节一致、历史语境保持原样」的判据，本轮只订正 §1「后端测试基线」
一行与 §2.1 / §2.2 / §2.3 的基线数字（分解数字按 §2.4 第 6 条一起重算），并把
`docs/evidence/pytest-baseline.txt` 换成这次实跑的原文。应用版本仍是 `1.0.19`、
数据表仍是 **24 张**、`WRK_*` 仍是 **43 个**；§6.4 / §6.5 / §6.6 里的 `707 → 754`、
`754 → 775`、`23 → 24 张表` 与 v1.0.19 发版记录都属历史语境，不回改。
前端测试同步从 19 项增加到 22 项（新增 3 条个人 Agent 的 E2E 用例：历史消息分区、
执行中分区、只有推理没有回答），前端没有单独维护测试计数基线，故 §2 只记后端数字。

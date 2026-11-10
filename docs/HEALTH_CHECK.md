# 项目体检与改进清单

> 体检对象：`guanmuya/wiki-rag-knowledge`（毕业项目），默认分支 `master`
> 体检基线：版本 `v1.0.17`，最近提交 `2026-11-07`，仓库提交总数 126
> 体检日期：本轮对话；产出形式：本文件（聊天回复只放摘要）
> 本轮边界：**只出体检结论与排好优先级的改进清单，不改产品代码、不创建实现任务**。清单经用户确认后才分批开工。

---

## 1. 体检怎么做的

不是凭印象打分，下面每条结论都能在仓库里找到对应文件或命令输出：

| 手段 | 具体做了什么 |
| --- | --- |
| 静态盘点 | 列全仓库 370 个路径；统计 `backend/app/` 41 个 Python 文件 / 14,686 行，`frontend/src/` 33 个 TS/TSX 文件 / 13,800 行，最大单文件 `llm.py` 1,253 行 |
| 运行时验证 | 实际执行 `python -m pytest backend/tests -q`（Python 3.13.11 / pytest 9.2.0），得出真实测试基线 |
| 文档交叉核对 | 把 README、`docs/*.md` 里的版本号、表数、并发数、锁超时、测试数、行号引用逐条回查代码 |
| 仓库治理检查 | 查 CI / lint / LICENSE / CHANGELOG 是否存在、`.gitignore` 覆盖范围、`.dsh-kanban.json` 状态、提交历史与截图体积 |
| 专项复核 | 后端代码与安全、前端与构建、文档与实验材料三条线并行只读复核（结论并入 §3、§4） |

测试夹具 `backend/tests/conftest.py:18-32` 使用独立临时目录并关闭 Worker 与预热，因此本轮跑测试不会污染 `data/`。

---

## 2. 总体结论

**项目本身是健康的，功能完成度与工程严谨度都高于一般毕业设计**：21 张表、双形态索引、两层融合检索、SSE 思考过程、sidecar 生命周期、写锁分档治理、个人 Agent 插件体系都落地了，并且配了 42 个测试文件和成套的验证脚本；代码里几乎没有 `TODO/FIXME` 残留（检索到的 17 处「占位」都是「pending 占位消息」这类正常业务措辞）。

**真正的短板不在代码，而在「交付证据链」**，集中在三处：

1. **实验只有方案、没有结果**。`docs/EXPERIMENT.md`（618 行）是纯实验方案，`docs/THESIS.md` §4 标题就是「实验方法（待实验）」，E1–E5 五个对照实验全部标注「待实验」，仓库里没有 `results.csv` / `results.json` / `docs/results/`。对一个毕业项目，这是答辩时最容易被追问的缺口。
2. **文档事实口径互相矛盾、且已落后于代码**。版本号、表数、Worker 并发数、SQLite 锁超时、测试基线在 README / DESIGN / EXPERIMENT / THESIS / HANDOFF 之间各说一套。
3. **没有自动化保障网**。仓库没有 `.github/`、没有任何 CI、没有 lint/format 配置；前端把 `playwright` 装进了开发依赖却没有测试脚本，只有 11 个一次性的 `verify-*` 验收脚本（`frontend/scripts/`）和 5 个专项脚本（`scripts/verify/`）。全量后端测试约 170 秒，靠人记得跑不可靠。

---

## 3. 证据台账（已核实）

### 3.1 运行时实测

| 项 | README 声称 | 本轮实测 | 结论 |
| --- | --- | --- | --- |
| 后端测试 | `535 passed, 1 skipped`（`README.md:9`） | **583 passed, 1 skipped，170.56 s** | README 基线已过时，少记 48 项 |
| 测试文件数 | `28 个文件，300 项`（`README.md:181`） | **42 个 `test_*.py` + 1 个 `conftest.py`**（43 个 `.py`）；文件内 **403 个 `def test_`**、**73 个 `@pytest.mark.parametrize`** | 目录结构段明显是早期快照；403 个函数经参数化展开正好解释 583 项 |
| 手工写死的基线散落各处 | `README.md:9` 写 535 | `docs/THESIS.md:437`、`docs/DESIGN.md:577`、`docs/EXPERIMENT.md:431`、`docs/HANDOFF.md:55,83,174,295,355,448` 分别写 157 / 192 / 200 / 171 / 93 / 82 / 59 等互不相同的 passed 数 | 全仓没有一份可信基线，且仓库内无任何测试报告佐证 535 |
| Python 环境 | 「仓库已带 .venv，可跳过创建」（`README.md:85-86`） | 当前 checkout 无 `.venv`；本机全局 Python 3.13.11 + pytest 9.2.0 可直接跑通 | 说明对新克隆者不成立 |

### 3.2 文档与代码漂移（逐条回查代码）

| 编号 | 文档写法 | 代码事实 | 影响 |
| --- | --- | --- | --- |
| D1 | `README.md:57`、`README.md:210`、`docs/EXPERIMENT.md:58`、`docs/DESIGN.md:25`、`docs/THESIS.md:33` 均写 `WORKER_CONCURRENCY=2` | `backend/app/config.py:26` 默认 **`1`**，注释明确说明「多开 worker 只会互相抢写锁」 | 答辩讲并发设计时口径错误 |
| D2 | `README.md:61`、`docs/EXPERIMENT.md:16`、`docs/EXPERIMENT.md:612`、`docs/DESIGN.md:25`、`docs/THESIS.md:33` 写 `busy_timeout=15000` | `backend/app/config.py:34-40` 已是三档：`INTERACTIVE=3000` / `WORKER=30000` / `PROGRESS=1000`；`docs/DESIGN.md:202` 自己又写 3s | 同一份 DESIGN 内部前后矛盾 |
| D3 | `docs/THESIS.md:5` 写「版本 `1.0.0`（`backend/app/main.py:23-24`）」 | 实际 `1.0.17`（`backend/app/main.py:36`、`frontend/package.json:3`） | 论文素材里的版本与行号都失效 |
| D4 | `docs/THESIS.md:45`、`docs/THESIS.md:63` 写「19 张表」 | `backend/app/models.py` 实际 **21 个 `__tablename__`**（`users` … `app_settings`） | 论文数据模型章节不准确 |
| D5 | `README.md:7` 写 `1.0.17`；`docs/HANDOFF.md:3` 顶部快照仍停留在 `v1.0.10`（2026-11-01） | 当前 `v1.0.17`（2026-11-07） | 交接文档首屏就是过期信息 |
| D6 | `README.md` §5 配置表 | 逐条比对：`config.py` 出现 38 个 `WRK_*` 变量，其中 **11 个未进 README**（`WRK_BUSY_TIMEOUT_INTERACTIVE/WORKER/PROGRESS`、`WRK_LOCK_RETRY_ATTEMPTS`、`WRK_LOCK_RETRY`、`WRK_AGENT_ENABLED`、`WRK_AGENT_PLUGIN_DIR`、`WRK_AGENT_WORKSPACE`、`WRK_AGENT_RUN_TIMEOUT`、`WRK_AGENT_HISTORY_MESSAGES`、`WRK_AGENT_HISTORY_CHARS`） | 配置项文档不完整 |
| D7 | `docs/THESIS.md:31`、`:57` 等大量 `文件:行` 引用 | 代码已多次重构，行号整体漂移（如 `main.py` 版本常量从 23 行移到 36 行） | 论文「代码依据」不可直接引用 |

### 3.3 毕业交付材料

| 编号 | 发现 | 证据 |
| --- | --- | --- |
| E1 | 实验方案完整（数据集、四个对照实验、指标定义、公平性陷阱、显著性检验方法），但**零结果数据** | `docs/EXPERIMENT.md` 618 行；`docs/THESIS.md:329` 标题「实验方法（待实验）」、`:337-341` E1–E5 全「待实验」、`:380` p 值「待实验」、`:390`「待实验清单」；仓库无 `results.csv` / `results.json` / `docs/results` |
| E2 | 硬件/环境信息留白 | `docs/EXPERIMENT.md:19`「CPU、内存、磁盘型号**待实测**」、`:3`「凡未能实测的一律标注『待实验』」 |
| E3 | 采集脚本在，但产出未入库 | `backend/scripts/collect_metrics.py`、`demo_data/questions.json` 存在；`results.csv` 不存在 |
| E4 | `docs/HANDOFF.md` 是 1,435 行的追加式历史快照，叠了 v1.0.0 → v1.0.10 多轮记录 | `docs/HANDOFF.md` 行数 1,435，`:3` 最新快照 v1.0.10 |

### 3.4 工程与仓库治理

| 编号 | 发现 | 证据 |
| --- | --- | --- |
| G1 | 无 CI、无 lint/format 配置 | `.github/`、`pyproject.toml`、`ruff.toml`、`setup.cfg`、`.pre-commit-config.yaml`、`.editorconfig` 均不存在 |
| G2 | 无 LICENSE / CHANGELOG / CONTRIBUTING | 均不存在（`docs/UPDATE.md` 承担了部分变更记录，但不是 CHANGELOG） |
| G3 | 后端依赖只写下界，不锁版本 | `backend/requirements.txt` 14 行全部为 `>=`；无 lock 文件、无开发/测试工具分组 |
| G4 | 前端有测试工具却无测试运行器 | `frontend/package.json:32` 有 `playwright`，但 `scripts` 只有 dev/build/typecheck，无 test；`frontend/scripts/` 下 11 个 `verify-*.mjs`/`.cjs`、`scripts/verify/` 下 5 个专项脚本，均为一次性截图/验收用途 |
| G5 | `.dsh-kanban.json` 已成为停滞的历史档案 | 153,147 字节**单行** JSON；50 张卡全部在 `Done`（`c4`）；158 条活动，最后一条 `2026-11-01T03:16:36Z`，而当前版本是 2026-11-07 的 v1.0.17 |
| G6 | 仓库提交了大量截图 | `docs/screenshots/` 157 个 PNG，合计约 12.9 MB |
| G7 | 快速开始写死本机路径 | `README.md:83` `cd /f/GItHub_Code/Wiki-Rag-Knowledge`，他人无法直接照做 |
| G8 | 后端存在超大模块 | `backend/app/services/llm.py` 1,253 行、`routers/kbs.py` 933 行、`services/qa_pipeline.py` 834 行、`services/wiki.py` 769 行、`services/retrieval.py` 748 行 |

---

## 4. 优先级改进清单

### 4.0 排序依据与两项假设

排序按 **「答辩/交付前是否必须闭环」＞「影响面 × 证据确凿度」＞「工作量」**。开工前先把两条开放问题按下面的假设定下来，若与实际情况不符，告诉我即可重排：

- **假设 A（改进范围）**：把**文档、实验数据、论文素材、仓库治理**都算进「改进」，不只算代码改动。理由：这是毕业项目，`docs/THESIS.md`、`docs/EXPERIMENT.md` 本身就是交付物，而本轮最大的风险恰好集中在这些材料上。
- **假设 B（时间点）**：按「答辩/交付前先闭环 P0」排序，未知具体日期。若已有明确答辩或提交时间点，P0 需压到该日期前完成，P1 顺延；告诉我日期我会重排。
- **两条高严重度安全项（P1-6、P1-7）放在 P1 而非 P0**，是因为本项目是本机单用户桌面程序、Agent 插件入口仅管理员可用，远程可利用性低；但**若答辩演示会接入真实模型、真实资料或联网，建议把它们提到批次一**，不要带着已知边界问题上台。

每项都给了**验收标准**，确认后可直接按批次拆成实现任务。

### 4.1 P0 — 答辩/交付前必须闭环

| 编号 | 改进项 | 为什么要做（证据） | 建议动作 | 验收标准 |
| --- | --- | --- | --- | --- |
| **P0-1** | 补齐实验数据并落库 | E1、E2、E3：五个对照实验全是「待实验」，仓库无任何结果文件 | 固定一种模型配置（建议 B 在线）与随机性参数，按 `docs/EXPERIMENT.md` 跑完 E1–E5；把原始结果（CSV/JSON）与汇总表提交到仓库（如 `docs/results/`）；在 `EXPERIMENT.md` 增「实验结果」章，在 `THESIS.md` §4 填实测值、p 值与硬件信息 | 仓库存在可复现的结果数据文件；`THESIS.md` §4 不再出现「待实验」；每个报告数字能追溯到结果文件的某一行；`collect_metrics.py` 的采集命令与参数写进文档 |
| **P0-2** | 统一文档事实口径与 API 契约 | D1–D7、§5.3：版本、表数、并发数、锁超时、行号引用各说一套；6 份文档写死互不相同的测试基线；`docs/API.md:334-345` 漏列 `discover` / `models/import` 两个已实现接口 | 以代码为唯一事实源，逐条订正 README / DESIGN / EXPERIMENT / THESIS / HANDOFF / API；测试基线只保留一处并指向 CI 输出；把 `文件:行` 改成「文件 + 符号名/章节」等不易漂移的锚点；补 `discover`、`models/import` 与 `/api/diagnostics/*` 的契约行 | 六份文档里 `WORKER_CONCURRENCY`、`busy_timeout`、版本号、表数、测试基线完全一致且与代码一致；`DESIGN.md` 内部不再自相矛盾；抽查 5 处 `文件:行` 均能定位到所述内容；`API.md` §8 覆盖 `routers/settings.py` 全部路由 |
| **P0-3** | 修正并固定测试基线说明 | §3.1：README 写 535 / 28 文件 / 300 项，实测 583 / 42 文件 | 更新 `README.md:9` 与 `README.md:181`；在文档中注明测试口径（命令、Python/pytest 版本、1 项 skip 的来源）；后续由 CI 自动产出数字，避免再次手工写死 | 全量 `pytest backend/tests -q` 通过且无新增 skip；README 数字与 CI 一次实际运行输出一致；注明测试环境与耗时 |
| **P0-4** | 刷新交接文档首屏 | D5、E4：`HANDOFF.md` 顶部快照停在 v1.0.10，全文 1,435 行 | 把 `HANDOFF.md` 顶部改成 v1.0.17 当前状态摘要（版本、测试基线、已知遗留、如何验证）；历史快照折叠到末尾或移入归档文件 | 打开 `HANDOFF.md` 前 20 行即可看到当前版本与当前状态，无需翻到历史段落 |

### 4.2 P1 — 工程化门禁与安全边界

| 编号 | 改进项 | 为什么要做（证据） | 建议动作 | 验收标准 |
| --- | --- | --- | --- | --- |
| **P1-1** | 建立最小 CI | G1：无 `.github/`、无 CI、无 lint 配置；测试约 170 s，靠人记得跑不可靠 | 加一个 GitHub Actions workflow：`pytest backend/tests -q` + `pnpm typecheck` + `pnpm build:web`；后端用 Python 3.13 固定版本；失败即阻断合并 | PR 触发后三项检查自动运行并全绿；README 带上状态徽标；一次故意改坏的提交能被 CI 拦下 |
| **P1-2** | 锁定依赖、补齐可复现环境 | G3、§5.2：14 个依赖全是 `>=`；`build-backend.mjs:140-145` `pip install pyinstaller` 无固定版本、`:185,202-205` `COLLECT_ALL` 按当前 venv 已装包动态决定；`.venv` 说法对新克隆者不成立（§3.1） | 用 `pip freeze` 或约束文件锁版本并固定 PyInstaller；把 dev/test 依赖分组；`README.md` §3.1 改成相对路径并明确「首次需自行创建 .venv」 | 在一台干净机器上按 README 一步步能装出可跑全量测试的环境；两台不同机器打出的后端 exe 可比对一致；CI 使用同一份锁文件 |
| **P1-3** | 给前端补真实自动化测试 | G4：`playwright` 已装但无 test 脚本，只有一次性 `verify-*` 脚本 | 选一个运行器（建议 vitest 做组件/工具函数单测 + 保留 Playwright 做关键路径 E2E）；先把「登录 → 建库 → 上传 → 问答」一条主链路固化成可重复用例，纳入 CI | `package.json` 有 `test` 脚本且在 CI 中通过；至少 1 条端到端用例可重复运行；`verify-*` 脚本标注为人工验收辅助而非测试 |
| **P1-4** | 后端超大模块拆分 | G8：`llm.py` 1,253 行、`kbs.py` 933 行等 | 按职责拆 `llm.py`（协议适配 / CallStats / 向量打包 / 离线降级）、`kbs.py`（CRUD / 文档 / 任务 / 成本）；每次拆分只搬代码不改行为 | 全量后端测试仍全绿；单文件行数明显下降；无循环依赖；改动以「行为不变」为验收前提 |
| **P1-5** | 前后端契约同步机制 | 契约靠手写 `frontend/src/types.ts` 与后端 `schemas.py` 对齐，无自动生成 | 从 FastAPI 的 OpenAPI 生成 TS 类型，或至少加一条 CI 检查比对关键字段 | 前后端字段增删能被检查发现；`types.ts` 有生成来源说明；类型检查通过 |
| **P1-6** | **高**｜让认证令牌可撤销 | §5.1：自研签名 token 无 `jti`/黑名单；`/auth/logout` 只写审计；改密与管理员重置密码都不使已签发 token 失效；TTL 720 分钟 | 引入 token 版本或 `jti` 并在鉴权时校验；改密/重置密码递增版本号；补回归用例 | 改密后旧 token 调任意需鉴权接口返回 401；logout 后旧 token 失效；三条均有测试；README/API 文档同步 |
| **P1-7** | **高**｜收敛个人 Agent 插件的执行边界 | §5.1：清单 `bins` 可把绝对路径直接解析成可执行程序；`.cmd/.bat` 走 `cmd.exe /c` 且参数被二次解析；`build_env` 白名单之后仍可被清单覆盖任意键（含 `PATH`/`PYTHONPATH`/`NODE_OPTIONS`）；`{api_key}` 允许进入 `run_args` | 限制 `bins` 只取 PATH 中的命令名并校验解析结果；消除 cmd 元字符注入；env 覆盖限白名单键；从命令行移除 API Key 并统一脱敏；`workspace` 强制落在 `AGENT_WORKSPACE_DIR` 内 | 恶意清单用例被拒绝；命令行与事件日志中不出现 API Key；`workspace` 越界返回 400；现有 Agent 插件与核心测试全绿 |
| **P1-8** | **中**｜中风险安全项与可观测性打包处理 | §5.1：`must_change_password` 后端从不校验（`deps.py` 无匹配）；登录无失败计数/限流；上传只校验扩展名、无魔数检测；密钥文件 `os.chmod(0o600)` 在 Windows 不设 ACL；`services/watchdog.py` 14 处 `except Exception: pass` 让日志轮转失败完全静默；无 alembic，schema 靠 `db.py` 手写「只加列」DDL 演进 | 后端强制初始口令修改、登录失败限流、上传魔数校验、密钥文件写入即收紧目录权限、关键 `except` 补日志、迁移引入版本号与 downgrade | 每条有对应测试；`must_change_password=true` 时后端拒绝业务接口；日志轮转失败有告警；老库升级路径可追溯 |
| **P1-9** | **中/高**｜Electron 渲染进程与更新链路加固 | §5.2：`main.cjs:587` `sandbox: false`；`frontend/index.html` 无 CSP 而 `Markdown.tsx:13-16,24` 用 `dangerouslySetInnerHTML`；`electron-builder.yml:69` `forceCodeSigning: false` + `updater.cjs:214-219` `autoDownload/autoInstallOnAppQuit` 全开 + `:198,231-232` `WRK_UPDATE_URL` 无 https 校验、`dev-app-update.yml:3-4` 为 `http://127.0.0.1:8899`；`main.cjs:267-273` 把初始管理员明文口令写入 `main.log` 且可经 `getLogs`/兜底页读取 | 评估开启 `sandbox: true`；加 CSP 并保留 DOMPurify；更新源强制 HTTPS（或书面记录明文源风险）并优先补签名校验；初始口令不落日志、首次改密后清除 | 交付配置里无明文 HTTP 更新源；日志中检索不到初始口令；Markdown 渲染正常；打包版启动与更新检查通过；`pnpm build:web` 通过 |
| **P1-10** | **中**｜前端类型契约与错误兜底 | §5.2：`types.ts` 手写且与 `preload.cjs` 已漂移（漏 `getFailure`、`openPath('install')`、`getInfo` 15+ 字段），靠 `as unknown as` 强转；`api.ts` 四份重复 fetch/超时/401 实现；无 React ErrorBoundary，任一页面抛错即整窗白屏 | 契约自动生成或加一致性检查（并入 P1-5）；统一 `request` 封装；加 ErrorBoundary 兜底页 | 强转消失；改一处鉴权规则全站生效；制造一次页面渲染异常时显示兜底页而非白屏 |

### 4.3 P2 — 可维护性与清理（可并行、可延后）

| 编号 | 改进项 | 为什么要做（证据） | 建议动作 | 验收标准 |
| --- | --- | --- | --- | --- |
| **P2-1** | 归档 `.dsh-kanban.json` | G5：153 KB 单行 JSON，50 卡全 Done，最后活动 2026-11-01 | 已全部完成且停滞，移入 `docs/archive/` 或删除；若想保留开发过程，导出成可读 Markdown 摘要 | 根目录不再有巨型单行 JSON；历史可查；git diff 不再被单行大文件淹没 |
| **P2-2** | `docs/screenshots/` 归档与正文引用 | G6、§5.3：157 个 PNG / 12.90 MB，但 `docs/*.md` 中 `![` 图片引用 **0** 处，全是开发过程 before/after 证据、与论文正文零关联 | 按「当前版本 UI」与「历史对比」分目录；把能支撑论文/设计说明的关键截图引用进 `THESIS.md` / `DESIGN.md`；历史对比图压缩或移出主分支 | 关键截图在正文有引用且路径有效；仓库体积可控；论文有配图 |
| **P2-3** | 补交付基础文件与 `.gitignore` 通用规则 | G2、§5.3：无 LICENSE / CHANGELOG / CONTRIBUTING / `.editorconfig`；`.gitignore:1-50` 覆盖良好但缺通用 `*.log` / `.tmp-*` / `*.key` / `*.pem`（体检时一度出现未跟踪的 `.tmp-health-pytest.log`） | 按毕业交付要求补 LICENSE、CHANGELOG、`.editorconfig`；补上四类通用忽略规则 | 文件存在且内容符合项目实际；CHANGELOG 至少覆盖 v1.0.0 → v1.0.17 的关键变更；新建临时日志/密钥文件不会误入 `git status` |
| **P2-4** | 清理写死的本机路径 | G7、§5.3：`README.md:83`、`EXPERIMENT.md:429,516,445-447`、`HANDOFF.md` 多处、`build.mjs:137,183,229`、`scripts/diag/repro_freeze_exe.py:21`、`prove_old_freeze.py:16` | 改成仓库根目录相对写法；错误提示改用「如何定位」而非具体盘符；补 Windows PowerShell 下的等价命令 | 新克隆者照 README 能跑通；全仓不再出现 `F:\GItHub_Code`、`C:\Users\Administrator` 等开发者个人路径（诊断脚本若必须保留则标注仅开发用） |
| **P2-5** | 一次性排障脚本标注与归档 | §5.3：`scripts/diag/` 3 个与 `scripts/verify/` 5 个已被 git 跟踪，另有 `live_check.py` / `inspect_db.py` / `check_questions.py` / `build_opening_report.py` / `ui_check.mjs` 等 | 统一移入 `scripts/dev-only/` 或加 `README.md` 注明「仅开发排障用、不属于交付物、不进 CI」；`.gitignore` 只保留其临时产物规则 | 目录用途一眼可辨；不影响任何构建与测试命令；`scripts/` 根目录只剩长期维护的入口脚本 |
| **P2-6** | 前端大文件与重复文案收敛 | §5.2：`styles.css` 2,913 行、`ModelsPage.tsx` 1,641、`ChatPage.tsx` 1,451；`main.cjs:60-103` / `BackendGate.tsx:37-58` / `updater.cjs:72-87` / `build.mjs:101-187` 错误文案映射表重复三套（注释自承认）；全仓无 i18n 框架 | 拆大组件；把错误文案映射抽成单一来源；中文文案集中管理（暂不引入完整 i18n） | 单文件行数明显下降；改一处文案四处同步生效；`pnpm typecheck` 与 `pnpm build:web` 通过 |

---

## 5. 专项复核补充（后端安全 / 前端 Electron / 文档契约）

> 三条只读复核线（后端代码与安全、前端与构建、文档与实验材料）的结论并入本节与 §4.2 的 P1-6 ~ P1-10、§4.3 的 P2 项。
> 复核只做静态阅读，未改任何文件；`{{...}}` 是仓库里的脱敏占位符，按原样引用，不代表真实密钥。
> 复核里有一条「60+ 个测试文件」的计数与实际不符：已按 `Get-ChildItem` 实测的 **42 个 `test_*.py` + 1 个 `conftest.py`** 为准（见 §3.1）。

### 5.1 后端代码与安全（已完成）

**先说好的一面**：全仓库 `TODO/FIXME/HACK/XXX/NotImplementedError` 与裸 `except:` 均为 0；无 `shell=True`、无用户输入拼接进 SQL；API Key 用 Fernet 加密落盘、列表接口只回脱敏值；`db.py` 把 `database is locked` 统一翻译成 503 + 中文提示；写锁分档、重试、进程守护都有成体系的设计与注释。整体质量明显高于一般原型。

**高严重度（对应 P1-6、P1-7）**

| 发现 | 证据 | 影响 |
| --- | --- | --- |
| 认证令牌不可撤销 | `app/security.py:112-131`、`app/routers/auth.py:68-72`；token 为自研 HMAC 签名（`{uid,role,exp,n}`），随机数 `n` 生成后从不存储校验、无 `jti`/黑名单；`/auth/logout` 只写审计；TTL `config.py:85` = 720 分钟 | 被盗 token 最长 12 小时内无法作废 |
| 改密/重置不使旧 token 失效 | `app/routers/auth.py:50-65`、`:143-157` | 凭证轮换形同虚设 |
| 插件清单可驱动任意本机命令 | `app/services/agent/plugins.py:395-405`、`:317-321` + `app/services/agent/runtime.py:535,550`；`bins` 允许绝对路径直接 `is_file()` 解析后 `Popen([path,*args])`；`.cmd/.bat` 走 `cmd.exe /c` 且参数被二次解析 | 能写 `agent_plugins/` 目录即可指定任意可执行程序，或借 `& | ^ %VAR%` 注入命令（静态推断，需 Windows 上实测确认） |

**中严重度（对应 P1-8、P1-1）**

- `app/serializers.py:103` + `app/deps.py`：`must_change_password` 只写入/输出、后端从不拦截，绕过前端即可用初始口令调用全部 API（已复核：`backend/app` 内该字段仅 7 处，无一处是鉴权判断）。
- `app/routers/auth.py:15-27`：登录无失败计数/限流/锁定。
- `app/services/parser.py:579-580` + `app/routers/kbs.py:242-253,380`：上传仅按扩展名白名单，`content_type` 只入库不校验，无魔数检测；`:247,496` 整文件一次性 `read()` 入内存。
- `app/services/watchdog.py` 共 14 处 `except Exception: pass`（`:145,164,175,200,220,242,268,362,498,503,512`），其中日志轮转失败完全静默——而日志正是「卡死取证」的唯一现场。另 `app/security.py:40,81,130`、`app/db.py:117-118,158-160,281-282,442-443` 也静默吞异常。
- `app/security.py:61-90` + `app/config.py:11-12`：密钥先明文 `write_bytes` 落盘后才 `os.chmod(0o600)`，Windows 下 chmod 不设 ACL，且密钥与业务库同在 `DATA_DIR` 下，备份会一起带走。`:78-84,151-157` 密钥长度不符时静默删重建、解密失败静默返回空串，已加密 API Key 无声失效。
- `app/main.py:135-141` + `app/config.py:89-92`：`allow_credentials=True` 且 `allow_methods/headers=["*"]`，白名单若被配成 `*` 则任意 Origin 可带凭据跨域访问。
- `app/services/agent/plugins.py:612-640`：`build_env` 白名单之后仍允许清单覆盖任意键（含 `PATH`/`PYTHONPATH`/`NODE_OPTIONS`）；`:365-373` 版本探测子进程未传 `env=`，继承后端全量环境；`:667-675` + `runtime.py:538,632` 脱敏只处理提示词不处理 `api_key`，密钥可能进命令行与事件日志。
- `app/routers/agent.py:90-100`：`_workspace` 不限制在 `AGENT_WORKSPACE_DIR` 内。
- `app/routers/agent.py:373-385` 原始异常文本回显进 500；`app/services/llm.py:633-635` 供应商响应正文前 600 字符进异常消息——同类失败在不同接口表现不一致，且可能把 URL/鉴权线索带进问答历史与审计。
- `app/db.py:388-429`：无 Alembic，schema 靠手写「只加列」DDL 演进，无版本号/downgrade。
- `backend/requirements.txt:1-14`：14 个依赖全为 `>=` 下限，无 `==`/上界/lock/哈希；`pytest` 混进生产清单。
- 结构：`app/services/llm.py` 1,253 行、`routers/kbs.py` 933 行、`qa_pipeline.py` 834 行等 10 个文件 ≥540 行；12 个函数 ≥90 行（`wiki.py:461` 266 行、`worker.py:187` 191 行、`ingest.py:73` 188 行）；`kbs.py:242-253` 与 `:492-500` 两套上传校验逐字重复；`llm.py:714-758` 三协议分支重复。
- `scripts/seed_demo.py:23,127,197-198`、`scripts/seed_expanded.py:33,170,364-365`：演示账号口令以占位符形式写死并打印到控制台，需确认发布/演示时是否注入真值；若未注入，所有演示账号共用固定口令。

**低严重度（记录，不单列任务）**：`parser.py:19-20,562-565` 的 csv/json/html/htm 解析分支因不在上传白名单而永不可达；`plugins.py:678` `clone_with()` 无人调用；`security.py:160-166` 脱敏泄露 API Key 首 3 尾 4 字符；`security.py:22` PBKDF2 240000 次低于 OWASP 2023 建议的 600000；`tests/conftest.py:18` `mkdtemp()` 临时目录不清理；`config.py:14-15,109-110` 模块导入即 `mkdir()`；`procguard.py:154-156` 裸文件句柄存全局；`diagnostics.py:94-105` 诊断接口运维信息面偏大。

**正面结论（无需改）**：`session_scope`/`get_db` 在 finally 关闭会话；文件名用 `Path(...).name` 归一、暂存名 `uuid4().hex`、目录由整数 ID 拼成，未发现用户可控的路径穿越；API Key 加密落盘链路完整；`WRK_*` JSON 打印是与 Electron 的 IPC 协议行而非调试残留；`backend/app` 内未发现写死的本机绝对路径或真实内网 IP。

**待运行时验证（不写进本轮任务，列为开工前确认项）**：插件清单注入在 Windows 上的实际可利用性、打包版 `WRK_CORS_ORIGINS` 取值、`download_document`（`kbs.py:606-611`）读取前是否应校验 `storage_path` 落在 `FILES_DIR` 内、`llm.py:660/761` 两处模型解析重复程度、依赖实际锁定版本是否有已知 CVE（需 `pip freeze` + `pip-audit`）。

### 5.2 前端与构建（已完成）

**正面（比预期好）**：`frontend/tsconfig.app.json:17-21`、`tsconfig.node.json:14` 开了 `strict` 与 `noUnusedLocals`/`noUnusedParameters`/`noFallthroughCasesInSwitch`/`noUncheckedSideEffectImports`；`frontend/src` 全域检索无 `any`、无 `@ts-ignore`/`@ts-expect-error`，只有 `AgentPage.tsx:156` 一条 `eslint-disable-next-line`——但项目并未安装 ESLint，该注释实际不生效。`pnpm-lock.yaml` 齐备（精确解析 electron 33.4.11 / react 18.3.1 / vite 6.4.5 / typescript 5.9.3 / playwright 1.64.1），`typecheck` 与 `build:web` 脚本都有。Electron 核心隔离到位：`main.cjs:583-589` `contextIsolation: true`、`nodeIntegration: false`、`webSecurity: true`；`:597` 打包版 `loadFile(dist/index.html)` 不加载远程内容；`:601-614` 拦截新窗口与导航白名单；`preload.cjs:4-22` 只经 `contextBridge` 暴露 9 个受限 IPC。`ELECTRON_RUN_AS_NODE` 已在 `build.mjs:55-63`、`dev.mjs:37-42` 主动清理。

| 发现 | 证据 | 严重度 |
| --- | --- | --- |
| 无自动化测试运行器 | `frontend/package.json:8-17` 脚本只有 dev/build/typecheck/start；`:32` 装了 `playwright` 核心包却无 `test` 脚本、无 `@playwright/test`/vitest/jest、无 `playwright.config` / `*.test.*` / `*.spec.*`；`frontend/scripts/` **11 个** `verify-*.mjs`/`.cjs`（复核里「14 个」是 `frontend/scripts/` 下全部脚本数，其中 3 个是 build-backend/build/dev）+ `scripts/verify/` 5 个专项脚本，均需手工起服务、手工设 env、未接入 CI | **高**（对应 P1-3） |
| 更新链路无签名且允许明文 HTTP 源 | `electron-builder.yml:69` `forceCodeSigning: false`、`:79-80` `signAndEditExecutable: true`（注释自述「无签名证书」）；`updater.cjs:198,231-232` 把环境变量 `WRK_UPDATE_URL` 直接当 generic feed、无 https 校验；`dev-app-update.yml:3-4` `url: http://127.0.0.1:8899`；`updater.cjs:214-219` `autoDownload=true` + `autoInstallOnAppQuit=true` | **中/高**（对应 P1-9）：明文源可被中间人同时替换 `latest.yml` 与安装包并自动下载安装 |
| 初始管理员明文口令落日志且可被渲染进程读 | `main.cjs:267-273` 捕获「密码：」行写入 `startupError` 并 `log('初始管理员信息：')` → `userData/logs/main.log`；`:696` `wrk:logs` 返回最近 200 行；`preload.cjs:7` 暴露 `getLogs`；`BackendGate.tsx:163,173` 在兜底页加载/复制日志 | **中**（对应 P1-9） |
| 渲染进程纵深防御不足 | `main.cjs:587` **`sandbox: false`**；`frontend/index.html` 无 CSP，而 `Markdown.tsx:13-16,24` 用 `dangerouslySetInnerHTML` 渲染用户/模型生成的 Markdown（已用 DOMPurify 净化）；`main.cjs:602,612` `shell.openExternal` 只判 `/^https?:/` 无域名白名单 | **中**（对应 P1-9） |
| 手写类型契约已漂移 | `frontend/src/types.ts:1` 自述「与 docs/API.md 对齐」但无 codegen；`api.ts:67-93` 手写 `Window.wrkDesktop` 漏 `getFailure`（`preload.cjs:9` 实际暴露）、`openPath` 联合类型漏 `'install'`（`preload.cjs:11`）、`getInfo` 只声明 4 字段而 `main.cjs:676-694` 返回 15+；`BackendGate.tsx:28-31,92`、`AboutPage.tsx:29-39,85,137` 只能靠 `as unknown as` 强转补 | **中**（对应 P1-5、P1-10） |
| 无 ErrorBoundary、请求逻辑重复四份 | `App.tsx:54-110`、`main.tsx:21-31` 无 `ErrorBoundary`；`api.ts:267-343` `request()`、`:345-385` `requestBlob()`、`:631-736` `askStream()`、`:874-922` `probeBackend()` 各自实现 fetch/超时/401/403/busy | **中**（对应 P1-10）：任一页面渲染抛错即整窗白屏；改一处鉴权规则要同步四处 |
| Python 侧打包不可复现 | `build-backend.mjs:140-145` `pip install pyinstaller` 无固定版本；`backend/requirements.txt` 全为 `>=`；`build-backend.mjs:185,202-205` `COLLECT_ALL` 依据当前 venv 「已安装包」动态决定 | **高**（对应 P1-2）：不同机器/时间打出的 exe 内容不同 |
| 构建失败提示写死开发者本机路径 | `frontend/scripts/build.mjs:137`、`:229`（`C:/Users/Administrator/.dsh/.../pnpm.mjs`）、`:183`（`F:\GItHub_Code\Wiki-Rag-Knowledge\.venv\Scripts\python.exe`）；同 `README.md:83` | **中**：换机后提示误导且泄露开发机路径（只是文案，实际 `build-backend.mjs:32` 用 `join(ROOT,'.venv')` 是对的） |
| 残留进程/目录锁只给「人话」提示 | `build.mjs:20-21,103-112,150-159,202` 提示 `taskkill /F /IM wrk-backend.exe`；`build-backend.mjs:213` `rmSync(DIST_DIR)` 无重试；运行时 `main.cjs:454-479` 才有 `killStaleBackends()` | **中**：CI/换机首次构建易随机失败 |
| 大文件与硬编码 | `styles.css` 2,913 行、`ModelsPage.tsx` 1,641、`ChatPage.tsx` 1,451、`AgentPage.tsx` 1,078、`api.ts` 924、`types.ts` 911；全仓无 i18n 框架，`utils.ts:50`、`UpdateCard.tsx:103`、`TasksPage.tsx:48` 写死 `'zh-CN'`，中文错误串还参与 `api.ts:184` 的业务判断 | **低/中**：当前仅中文可接受，但耦合逻辑 |
| 依赖较旧 | Electron 33（官方只维护最新 3 版）、React 18、Vite 6、electron-builder 25；`pnpm-workspace.yaml:5-6` `strictDepBuilds:false` / `verifyDepsBeforeRun:false` | **中**：需查证具体 EOL/CVE（列在待验证） |

**待运行时验证**：`main.cjs:561-572` `isAppFileUrl` 先 `decodeURIComponent` 再 `startsWith(root+'/')`、未 `path.resolve` 归一化，能否被百分号编码的 `..` 绕过；Electron 33 / React 18 / Vite 6 的具体 EOL 与 CVE；交付包里 `update-config.json` 的 `sources` 是否被替换、是否强制 HTTPS；`sandbox:false` 是否因未发现的 Node 依赖而必需；初始口令是否真的长期留在 `main.log`。

### 5.3 文档与实验材料（已完成）

**代码与 API 契约主体同步度其实不错**：抽查 setup/auth/users/kbs/documents/tasks/wiki/conversations/ask/ask-stream/chat-models/search-preview/cost/usage/settings/agent 共 30+ 接口，路径与 `routers/*.py` 装饰器逐一对得上；`README.md:163`「21 张表」与 `models.py` 的 21 个 `__tablename__` 一致；`README.md:7`、`main.py:36`、`package.json:3` 三处版本均为 `1.0.17`。**问题集中在「手工写死的事实」和「毕业材料」**。

| 发现 | 证据 | 严重度 |
| --- | --- | --- |
| 测试基线被手工写死进 6 份文档，值互不相同 | `README.md:9` 535；`THESIS.md:437` 157；`DESIGN.md:577`；`EXPERIMENT.md:431`；`HANDOFF.md:55,83,174,295,355,448` 出现 200/192/171/93/82/59 | **高**：无单一可信基线，仓库内无任何测试报告佐证 535 |
| `docs/API.md` 漏列两个已实现接口 | `docs/API.md:334-345` §8 模型设置表未列 `POST /api/settings/providers/{id}/discover`（`routers/settings.py:69`）与 `POST .../models/import`（`settings.py:88`）；二者 `README.md:281` 有描述、代码有路由 | **中**：按契约对接会漏功能 |
| 运维诊断接口未纳入契约 | `docs/API.md:4,6` 只在正文提到 `/api/diagnostics`，实际有 `GET /api/diagnostics/lock`（`diagnostics.py:94`）与 `/process`（`:102`） | **低** |
| README 的 `文件:行` 大面积漂移 | 抽查 8 处、4 处指错：`README.md:17,375` 引 `config.py:27` 说格式白名单，实际 `ALLOWED_SUFFIXES` 在 `config.py:49`；`:30,119` 引 `main.py:27`/`:27-49` 说首次引导，实际 `bootstrap_admin()` 在 `main.py:39`；`:22` 引 `retrieval.py:264` 说加权 RRF，实际 `rrf_fuse:285`/`channel_weights:308`；`:139` 引 `build.mjs:53-57` 说删 `ELECTRON_RUN_AS_NODE`，实际 `delete` 在 `:61`。全文 60+ 处引用 | **中**（对应 P0-2） |
| 论文素材只有方法与探针数字，无结果、无图表 | `docs/THESIS.md:329` 标题即「实验方法（待实验）」，`:331`「不填任何未跑出的数字」，`:337-341` E1–E5 全「待实验」；`:430-437` 仅少量探针数字（分块块数、9 条查询耗时 min 6.0/P50 6.8/max 14.3 ms、157 passed）；全文 **0 张图、0 个 `![`、0 个 mermaid** | **高**（对应 P0-1） |
| 实验方案自承「预期不是实测」 | `docs/EXPERIMENT.md:546`「以下均为**待验证的预期，不是实测结果**」；`:19` 硬件「待实测」；唯一一组实测是 `:238-250` 的表格分块三策略数字；`:465` 自承 `seed_expanded` 两个过期断言；全仓 **0 个 `.csv`** | **高**（对应 P0-1） |
| 可复现性有基础、缺结论 | 正面：`demo_data/questions.json` **44 题 Q01–Q44**、`demo_data/fixtures/` 6 个样本、`collect_metrics.py` 支持 `--repeat/--mode`、指标定义完整（Hit@k / MRR / 正确性三档 / 证据支持率 / Cohen's κ）；负面：无结果文件、**全仓无 `random.seed`/`set_seed`**、E3/E4 需改代码（`EXPERIMENT.md:230,236`）、路径写死 | **中**（对应 P0-1） |
| 交接文档首屏过期且仍列未完成项 | `docs/HANDOFF.md:3` 最新快照 `v1.0.10`（2026-11-01），落后 `v1.0.17` 七个版本；全文 1,435 行追加式历史；`:1430-1435` 仍列「仍未完成」4 项；`:372` health 返回 `1.0.0` | **高**（对应 P0-4） |
| 文档引用了不存在的脚本路径 | `docs/DESIGN.md:248` 引 `scripts/inspect_chunks.py`，实际在 `backend/scripts/inspect_chunks.py`；`DESIGN.md:77`「IndexVersion 只写不读」论断属实，但 `ingest.py:138/:249`、`wiki.py:602`、`models.py:266` 行号已失效 | **低/中** |
| 写死本机路径散落文档与脚本 | `README.md:83`、`:91`（文字写 `node <pnpm.mjs 路径>`，实际 `scripts/dev.sh:25` 是 `npx --yes pnpm@9 install`）；`EXPERIMENT.md:429,516,445-447`（`F:\wrk_exp\data`）；`HANDOFF.md` 多处；`scripts/diag/repro_freeze_exe.py:21`（`E:\Programs\...`）、`prove_old_freeze.py:16`（`C:\Users\Administrator\...`） | **中**：直接阻塞他人一键复现 |
| 仓库治理 | `.github/` 不存在，无 pytest.ini / pyproject.toml / ruff / ESLint / Prettier / tox / Makefile / `.editorconfig`；无 LICENSE / CONTRIBUTING / CHANGELOG；`.gitignore:1-50` 覆盖良好（`git ls-files` 违规跟踪文件 0）但缺通用 `*.log` / `.tmp-*` / `*.key` / `*.pem`；`.dsh-kanban.json` 单行 153,147 字节（约 103,246 字符），50 卡全 Done、158 条活动、最后一条 2026-11-01；126 次提交中 75 次集中在 2026-11-07 一天，最近 60 条里 32/34 非 merge 提交符合 `type(scope):` 规范（94%） | **中**（对应 P1-1、P2-1、P2-3） |
| 截图与论文零关联 | `docs/screenshots/` 157 个 PNG、13,529,500 字节 ≈ 12.90 MB、64 次提交触及，但 `docs/*.md` 中 `![` 图片引用 **0** 处；全是开发过程 before/after 证据 | **低/中**（对应 P2-2）：体积合理、不臃肿，但没进正文 |
| 一次性排障脚本已入库 | `scripts/diag/` 3 个、`scripts/verify/` 5 个均被 git 跟踪，另有 `live_check.py` / `inspect_db.py` / `check_questions.py` / `build_opening_report.py` / `ui_check.mjs` 等；`.gitignore:47-50` 只为其临时目录打补丁 | **低/中**（对应 P2-5） |

**待运行时验证**：`EXPERIMENT.md:238-250` / `DESIGN.md:250-254` 的表格分块「实测」数字能否由当前 `backend/scripts/inspect_chunks.py` 复现；`docs/UPDATE.md` 的发版流程是否仍适用于 v1.0.17（需对照 `frontend/electron/update-config.json` 与最近 Release）。

---

## 6. 分批开工建议

确认本清单后，按下面顺序拆实现任务（**本轮不拆、不派、不改代码**）：

| 批次 | 范围 | 目标 |
| --- | --- | --- |
| 批次一 | P0-1、P0-2、P0-3、P0-4 | 答辩材料闭环：有实验数据、文档口径一致、基线真实 |
| 批次二 | P1-1、P1-2、P1-3 | 把「能自动验证」立起来：CI + 锁版本 + 前端测试 |
| 批次三 | P1-4 … P1-10 | 结构优化、安全边界与前端工程化（行为不变为前提） |
| 批次四 | P2-1 … P2-6 | 清理与交付包装，可与答辩准备并行 |

批次一与批次二建议不要并行：P0-2 会改文档事实口径，P1-1 的 CI 输出又会成为新的事实来源，先后做更省返工。

---

## 7. 本轮明确没做的事

- 没有修改任何产品代码、配置或现有文档；本文件是唯一新增内容。
- 没有创建任何实现任务 / 待办卡片；按约定等用户确认清单后再按批次拆。
- 没有跑前端构建、打包或 Electron 安装包验证（本轮只做只读体检与后端测试基线验证）。
- 没有把 `.dsh-kanban.json`、截图或 `HANDOFF.md` 做任何删改，只在清单里提出建议。

# 交接说明

> **当前版本 v1.0.19（2026-11-11）｜当前状态：答辩材料 P0 已闭环，功能可交付；桌面安装包已构建，Release 待合并后发布**
>
> | 项 | 当前状态 |
> | --- | --- |
> | 版本 | `1.0.19`（`backend/app/main.py` 的 `VERSION`、`frontend/package.json` 的 `version`） |
> | 桌面安装包 | `wiki-rag-knowledge-1.0.19-x64-setup.exe`（NSIS，未签名，Win 会提示 SmartScreen）；产物指标与上传命令见 `docs/evidence/release-v1.0.19-build.txt` 与 `docs/RELEASE_NOTES_v1.0.19.md` |
> | 后端测试 | 全量 `python -m pytest backend/tests -q` **通过、无新增 skip**；计数口径与一次真实运行输出见 `docs/FACTS.md` §2（全仓唯一记录，本首屏不复述数字） |
> | 实验数据 | **配置 A 离线结果已落库**（`docs/results/` 16 个文件、`docs/EXPERIMENT.md` §8、`docs/THESIS.md` §4）；配置 B 在线、人工标注与实际 p 值仍按约定标「待实验」 |
> | 文档事实口径 | 已统一到 `docs/FACTS.md`（版本、表数、并发、锁超时、测试基线、代码锚点）；六份文档只放链接 |
> | 交接首屏 | 本文件前 20 行即当前状态；1,435 行历史快照已移入 `docs/archive/HANDOFF_HISTORY.md` |
> | 权威事实源 | **代码**；数值口径见 `docs/FACTS.md`；改进清单见 `docs/HEALTH_CHECK.md` |

> **一句话**：打开这份文件不需要翻历史段落——上面这张表就是当前状态。
> 历史记录（v1.0.0 → v1.0.10 的追加式快照）在归档文件里，其中的版本号、测试数、
> `文件:行` 行号、`busy_timeout`、`WORKER_CONCURRENCY` 都是当时的读数，**不是当前值**。

---

## 1. 已知遗留（按优先级）

### 1.1 仍标「待实验」——不是文档错误

本机没有在线模型配置与凭据（`GET /api/meta` 实测 `offline_models=true`），
第一波只能跑配置 A 离线（哈希向量 + 抽取式生成）。因此以下各项如实保留
「待实验」，不填推测值（依据 `docs/EXPERIMENT.md:3` 的仓库约定）：

- 配置 B（真实 embedding + 在线生成）下 E1–E5 的全部指标；
- 正确性三档、证据支持率、引用精确率、拒答正确率（需两人独立标注 + Cohen's κ）；
- Wilcoxon 符号秩 / McNemar 检验的实际 p 值；
- 真实 embedding 下的检索耗时 P50/P95；
- CPU 具体型号（本机为虚拟机，系统报告 `Not Available`）。

清单同步见 `docs/EXPERIMENT.md` §8.6、`docs/THESIS.md` §4.5、
`docs/results/summary.md` 末节、`docs/results/README.md` §8。

### 1.2 工程化门禁（批次二，**已闭环**）

`docs/HEALTH_CHECK.md` §4.2 的 P1-1 ~ P1-3 已全部落地：

- **CI**：`.github/workflows/ci.yml`，PR / push 到 `master` 自动跑五项检查（后端全量测试、前后端契约一致性、前端类型检查、前端 Web 构建、前端自动化测试），任一失败即阻断合并；
- **依赖锁定**：`backend/requirements.txt`（运行时依赖全部 `==`）、`backend/constraints.txt`（传依赖）、
  `frontend/pnpm-lock.yaml`；CI 用 `pnpm install --frozen-lockfile`；
- **前端测试运行器**：`frontend/tests/`（Playwright，离线确定性 mock），`pnpm test` 已接入 CI。

本机一次全绿实跑见 `docs/evidence/gates-2026-11-11.txt`；v1.0.19 发版前又整跑一遍，
输出见 `docs/evidence/release-v1.0.19-build.txt` §3（v1.0.18 那次是 `release-v1.0.18-build.txt` §3，属历史）。

### 1.3 结构、安全与清理（批次三 / 四，**已闭环**）

`kbs` 路由按职责拆分、签名令牌可撤销、个人 Agent 插件执行边界收紧、Electron 渲染进程
加固（CSP / `sandbox` / 更新源 HTTPS 校验）、`.dsh-kanban.json` 与历史截图归档、
写死开发机路径清理等 P1-8 / P2 项均已完成，逐条变更见 `CHANGELOG.md` 的 `[1.0.18]` 一节；
完整清单与验收标准见 `docs/HEALTH_CHECK.md` §4.2、§4.3。
原始 `HANDOFF.md` 末尾「仍未完成」4 项（Wiki 版本历史正文、历史消息字段、
模型 `params` 提示、检索调试保存实验）随历史快照一并归档，仍可在
`docs/archive/HANDOFF_HISTORY.md` 查到。

---

## 2. 如何验证（从仓库根目录执行）

```bash
# 后端全量测试（自带临时数据目录，不污染 data/）
python -m pytest backend/tests -q

# 前端类型检查与构建
cd frontend && pnpm typecheck && pnpm build:web

# 重跑第一波的离线实验结果（不需要起服务、不需要模型凭据）
PYTHONIOENCODING=utf-8 python backend/scripts/run_experiments.py

# 独立复核表格分块探针数字
PYTHONIOENCODING=utf-8 python backend/scripts/inspect_chunks.py
```

- 测试基线的命令、环境、原始输出与 skip 来源：**`docs/FACTS.md` §2** 与
  `docs/evidence/pytest-baseline.txt`。
- 实验结果的人读汇总与复现命令：**`docs/results/summary.md`** 与 `docs/results/README.md`。
- 1 项 skip 来自 `backend/tests/test_tables.py` 的 `pytest.importorskip("reportlab", ...)`
  （可选依赖未安装），不是漏测。
- 文档口径自检：六份文档里不应再出现互不一致的 `WORKER_CONCURRENCY`、
  `busy_timeout`、版本号、表数或 passed 数；权威值见 `docs/FACTS.md` §1。

---

## 3. 文档地图

| 文件 | 内容 |
| --- | --- |
| `docs/FACTS.md` | **统一事实口径（单一事实源）**：版本、表数、并发、锁超时、测试基线、代码锚点约定 |
| `docs/HEALTH_CHECK.md` | 项目体检结论与排好优先级的改进清单（P0 → P2） |
| `docs/HANDOFF.md` | 本文件：当前状态首屏 |
| `docs/archive/HANDOFF_HISTORY.md` | 历史快照归档（v1.0.0 → v1.0.10，历史读数，非当前事实） |
| `README.md` | 项目说明、快速开始、配置项、接口速览、测试与验收 |
| `docs/DESIGN.md` | 系统设计说明 |
| `docs/API.md` | 接口契约 |
| `docs/EXPERIMENT.md` | 实验方案（§1–§7）与离线实测结果（§8） |
| `docs/THESIS.md` | 论文可复用章节素材（§4 为实验方法与结果） |
| `docs/results/` | 第一波实验原始数据、汇总表、数据字典与复现命令 |
| `docs/evidence/pytest-baseline.txt` | 测试基线一次真实运行的完整 stdout |
| `docs/evidence/` 下另两份 | `gates-2026-11-11.txt`：五项门禁本地实跑记录；`wrk-count-2026-11-11.txt`：`WRK_*` 可调项条数核准与旧数字扫查 |
| `docs/RELEASE_NOTES_v1.0.19.md` / `docs/evidence/release-v1.0.19-build.txt` | v1.0.19 交付说明（Release 正文 + 上传命令）/ 构建记录（产物大小、`latest.yml`、sha512、门禁） |
| `docs/RELEASE_NOTES_v1.0.18.md` / `docs/evidence/release-v1.0.18-build.txt` | v1.0.18 交付说明与构建记录（历史，已发布） |
| `docs/PRICING.md` | 模型参考单价：口径与来源、归一化匹配、取价优先级、维护流程 |
| `docs/AGENT_PLUGINS.md` / `docs/UPDATE.md` | 个人 Agent 插件规范 / 自动更新发版流程 |

---

## 4. 本波（第二波）改了什么

按用户确认的「两波」阵容，第二波接手第一波的 `docs/results/` 与
`docs/EXPERIMENT.md` §8 / `docs/THESIS.md` §4，完成体检清单的
**P0-2（统一文档事实口径与 API 契约）、P0-3（修正并固定测试基线说明）、
P0-4（刷新交接文档首屏）**：

- 六份文档的并发数、锁超时、版本号、表数、测试基线统一到 `docs/FACTS.md`；
- 测试基线从 8 个互不相同的值收敛为一处，并附一次真实运行输出；
- `docs/API.md` §8 补齐 `discover` / `models/import` 与 `/api/diagnostics/*` 契约行；
- `docs/DESIGN.md` 内部 `15000` 与 `3s` 的自相矛盾消除；
- 代码引用从易漂移的 `文件:行` 改为「文件 + 符号名 / 章节」；
- 本文件首屏改为 v1.0.17 当前状态，历史快照归档。

第一波的实验结果与「待实验」标注**原样保留**，未覆盖、未删除；
本波未改动任何产品代码。

# 变更记录（CHANGELOG）

本文件只记录**版本 → 用户或维护者能感知的变化**，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

- **数字不在这里写**：版本号、测试基线、数据表数、并发与锁超时等口径只在 [`docs/FACTS.md`](docs/FACTS.md) 维护一处，本文件不复述。
- **v1.0.10 及以后**按 git tag 区间逐条整理，可用 `git log v1.0.15..v1.0.16` 之类的命令核对。
- **v1.0.2 ~ v1.0.9** 的开发历史在仓库里被压缩过：这 8 个 tag 指向同一个提交 `911759a`，
  逐版本差异已无法从 git 还原，因此只记录能对上的里程碑，并注明出处（看板归档 / 文档）。
- 发版操作步骤见 [`docs/UPDATE.md`](docs/UPDATE.md)；历史交接快照见 [`docs/archive/HANDOFF_HISTORY.md`](docs/archive/HANDOFF_HISTORY.md)。

---

## [1.0.20] - 2026-11-11

> 本版区间是 `v1.0.19` 之后合入 `master` 的部分，用 `git log v1.0.19..HEAD` 可核对；
> `v1.0.20` tag 由平台在合并落地后打在 `master` 的合并提交上，本节不预设 tag 存在。
> 测试计数不在本文件复述，见 `docs/FACTS.md` §2。

### 新增

- **个人 Agent 把思考过程与最终回答分开显示**：模型推理与工具尝试不再整段铺在回答气泡里，
  改为收进默认折叠的「思考过程」卡片，回答气泡只保留最终答案；需要时可展开查看过程
- **只有推理没有回答时明确标成未完成**：整轮只有思考、没有最终答案的运行不再显示成
  「回答完毕」，而是明确提示「这次执行只产出了思考过程，没有给出最终回答」，并归入未完成
- **旧消息不重解析**：历史消息按落库时的内容照常展示，不重新拆分思考/回答（库里没有原始事件流），
  新口径只对之后产生的消息生效

### 变更

- 个人 Agent 的 Codex 核心改用清单字段 `answer_item_types` 声明「哪些事件属于回答」：
  内置 Codex 声明 `agent_message`，其余推理 / 命令 / 文件改动等事件一律归「思考过程」；
  事件口径见 `docs/AGENT_PLUGINS.md` §3.4

### 发布

- 版本号递增到 `1.0.20`（`backend/app/main.py` 的 `VERSION`、`frontend/package.json` 的 `version`、
  `docs/FACTS.md` §1 三处同步）；`README.md`、`docs/HANDOFF.md`、`docs/THESIS.md` 里写当前版本的
  那几句同步改数字（历史快照、历史证据、历史 CHANGELOG 段落不动）
- 产出 Windows NSIS 安装包 `wiki-rag-knowledge-1.0.20-x64-setup.exe` 与更新源三件套
  （`latest.yml` + 安装包 + `.blockmap`）；产物大小、`latest.yml` 原文、安装包 sha512 与五项门禁的
  实跑输出见 `docs/evidence/release-v1.0.20-build.txt`
- 交付说明（Release 正文草稿 + 上传与校验命令）见 [`docs/RELEASE_NOTES_v1.0.20.md`](docs/RELEASE_NOTES_v1.0.20.md)：
  Release 资产在 `master` 合并、tag `v1.0.20` 打上之后再上传，本版不预先建 tag
- 清理 v1.0.17 遗留的空草稿 Release，并补记 v1.0.19 三件资产的上传与校验；证据见
  `docs/evidence/release-v1.0.17-draft-cleanup.txt` 与 `docs/evidence/release-v1.0.19-publish.txt`
- 安装包**没有代码签名证书**，Windows 首次安装会弹 SmartScreen「未知发布者」；
  风险与后续步骤仍是 `docs/UPDATE.md` §6 的书面记录，未变

---

## [1.0.19] - 2026-11-11

> 本版区间是 `v1.0.18` 之后合入 `master` 的部分，用 `git log v1.0.18..HEAD` 可核对；
> `v1.0.19` tag 由平台在合并落地后打在 `master` 的合并提交上，本节不预设 tag 存在。
> 测试计数不在本文件复述，见 `docs/FACTS.md` §2。

### 新增

- **管理员用量页重做**：六张指标卡、可切换的趋势图（调用次数 / Token / 估算费用）、
  按来源与核心拆分的汇总、单价设置弹窗、页脚口径说明；页面拆成
  `UsageMetrics` / `UsageTrend` / `UsageBreakdown` / `PricingEditor` 与共用的格式化函数，
  刷新期间保留旧数据不清屏
- **个人 Agent 用量进入统计**：`/api/usage` 把个人 Agent 单列成一个来源、并按核心拆分；
  问答落库时补记所用模型，无法计价的用量单独列出，不混进总额
- **模型参考价目录与单价覆盖**：内置一份带来源链接与查证日期的参考价（`docs/PRICING.md`），
  新增 `model_prices` 表与 `/api/settings/pricing` 三个接口（`GET` 列表、`PUT` 按模型覆盖、
  `DELETE` 恢复内置价）；页面上的金额一律标注「按单价估算」，不冒充账单
- **测试**：新增 `offline_llm` 离线夹具；新增用量页与「个人 Agent 页不再出现知识库入口」的回归测试

### 修复

- **个人 Agent 的 Codex 核心改用应用独立配置目录**（`CODEX_HOME`）：不再继承用户 `~/.codex`
  里的提示词与个人化配置，skills 以只读方式链接进来
- **提示词压成单行发送**：`codex exec` 只保留首行，此前多行提示会被截断
- **登录锁定偶发多报 1 秒 `Retry-After`**（浮点尾巴取整）
- **两条知识库 Agent 用例不再依赖真实 DNS**：原先真实出网调用吃掉 `_wait_run()` 的 15 秒预算，
  在 DNS 慢的机器上稳定失败；等待时长未改，只换夹具

### 变更

- **个人 Agent 下线知识库核心**：个人 Agent 页去掉知识库入口与检索文案，创建 / 更新会话时
  不再传 `kb_ids`；已存在的知识库会话仍可打开（核心名按 `core_label` 显示），并可直接切换到 CLI 核心

### 发布

- 版本号递增到 `1.0.19`（`backend/app/main.py` 的 `VERSION`、`frontend/package.json` 的 `version`、
  `docs/FACTS.md` §1 三处同步）；`README.md`、`docs/HANDOFF.md`、`docs/THESIS.md` 里写当前版本的那几句
  同步改数字（历史快照不动；`docs/screenshots/README.md` 只订正「哪两张图已过时」的说明，图片本身未动）
- 产出 Windows NSIS 安装包 `wiki-rag-knowledge-1.0.19-x64-setup.exe` 与更新源三件套
  （`latest.yml` + 安装包 + `.blockmap`）；产物大小、`latest.yml` 原文、安装包 sha512 与五项门禁的
  实跑输出见 `docs/evidence/release-v1.0.19-build.txt`
- 交付说明（Release 正文草稿 + 上传与校验命令）见 [`docs/RELEASE_NOTES_v1.0.19.md`](docs/RELEASE_NOTES_v1.0.19.md)：
  Release 资产在 `master` 合并、tag `v1.0.19` 打上之后再上传，本版不预先建 tag
- 安装包**没有代码签名证书**，Windows 首次安装会弹 SmartScreen「未知发布者」；
  风险与后续步骤仍是 `docs/UPDATE.md` §6 的书面记录，未变

---

## [1.0.18] - 2026-11-11

> 本版是**发版版**：只递增版本号（`backend/app/main.py` 的 `VERSION`、`frontend/package.json`
> 的 `version`、`docs/FACTS.md` §1 三处同步为 `1.0.18`）并产出 Windows 安装包，不改业务逻辑。
> 下面的改动区间是 `v1.0.17` 之后合入 `master` 的部分，用 `git log v1.0.17..HEAD` 可核对。
> `v1.0.18` tag 由平台在合并落地后打在 `master` 的合并提交上，本节不预设 tag 存在；
> 测试计数不在本文件复述，见 `docs/FACTS.md` §2。

### 新增

- CI：PR / push 到 `master` 自动跑五项检查（后端全量测试、前后端契约一致性、前端类型检查、前端 Web 构建、前端自动化测试），任一失败即阻断合并
- 前端 E2E 自动化测试（`frontend/tests/`，Playwright，离线确定性 mock），并接入 CI
- 前后端契约一致性检查（`pnpm check:contract`）
- 可复现的依赖锁定：`backend/constraints.txt`、`frontend/pnpm-lock.yaml`（CI 用 `--frozen-lockfile`）
- 文档：新增 [`docs/FACTS.md`](docs/FACTS.md)（唯一数值口径）、[`docs/HEALTH_CHECK.md`](docs/HEALTH_CHECK.md)（体检与改进清单）
- 后端：`kbs` 路由按职责拆成 documents / tasks / cost 三个模块
- 前端：统一请求封装（超时、401、错误语义集中一处）

### 修复

- 个人 Agent 插件的执行边界：`bins` 只从 PATH 解析、消除 cmd 元字符二次解析、env 覆盖限白名单、API Key 不再进命令行、`workspace` 强制落在工作目录内
- 桌面端：渲染进程 CSP / `sandbox`、更新源 HTTPS 校验，初始口令不再写入日志
- 认证：签名令牌支持撤销（改密 / 重置密码 / 登出后旧令牌立即失效）

### 变更

- **仓库治理（P2 批次四）**：`.dsh-kanban.json` 与历史截图归档到 `docs/archive/`；一次性排障脚本收进 `scripts/dev-only/`；
  补齐 `LICENSE` / `CHANGELOG` / `CONTRIBUTING` / `.editorconfig`；清理文档与构建提示里写死的开发机路径
- **删除一处纯重复副本**：`scripts/dev-only/legacy/frontend-ui_check.mjs`（当年 `frontend/ui_check.mjs` 的字节级重复副本，
  与现役 `scripts/dev-only/ui_check.mjs` 逻辑一致、无人引用），经确认后 `git rm`；历史截图**全部保留**，不做删除
- **文档口径收尾**：`docs/FACTS.md` 的可调项条数按代码重数并订正（原值与它自己的算式对不上），
  并补上可复现的数法与旧数字扫查登记；命令、输出与逐条命中见 `docs/evidence/wrk-count-2026-11-11.txt`。
  本轮只改文档，`docs/archive/**` 与代码零改动
- **补建版本 tag**：`v1.0.17`（轻量 tag，与 `v1.0.0`～`v1.0.16` 同口径，指向 master tip `d5ffe20`）。
  下面 `[1.0.17]` 一节原先的注记称该版本没有对应 tag，补 tag 后已不成立，本轮一并订正

### 发布

- 版本号递增到 `1.0.18`（三处口径同步），`docs/FACTS.md` §1 的「应用版本」随之更新；
  `README.md`、`docs/HANDOFF.md`、`docs/THESIS.md` 里写当前版本的那几句同步改数字（历史快照、
  实验结果与截图记录**不动**）
- `docs/HANDOFF.md` 的「已知遗留」按现状订正：批次二的工程化门禁（CI / 依赖锁定 / 前端测试）与
  批次三、四的结构安全项都已闭环，不再写成「尚未开工」
- 产出 Windows NSIS 安装包 `wiki-rag-knowledge-1.0.18-x64-setup.exe` 与更新源三件套
  （`latest.yml` + 安装包 + `.blockmap`）；构建环境、产物大小、`latest.yml` 原文、安装包 sha512
  与五项门禁的实跑输出见 `docs/evidence/release-v1.0.18-build.txt`（后端门禁一次全绿，
  第二次实跑撞上 `test_agent_runtime` 的 15 秒等待与本机慢 DNS，该文件 §3.1 有根因与判定）
- 交付说明（Release 正文草稿 + 上传命令）见 [`docs/RELEASE_NOTES_v1.0.18.md`](docs/RELEASE_NOTES_v1.0.18.md)：
  Release 资产在 `master` 合并、tag `v1.0.18` 打上之后再上传，本版不预先建 tag
- 安装包**没有代码签名证书**，Windows 首次安装会弹 SmartScreen「未知发布者」；
  风险与后续步骤仍是 `docs/UPDATE.md` §6 的书面记录，未变

---

## [1.0.17] - 2026-11-07

> 本次只 bump 版本号并合并功能分支。`v1.0.17` 是轻量 tag（与 `v1.0.0`～`v1.0.16` 同口径，无附注对象），
> 指向创建时的 master tip `d5ffe20`：用 `git log v1.0.16..v1.0.17` 可核对本版改动；
> 同一条区间也包含补 tag 之前已合入 master、记在上文 `[1.0.18]` 一节的那部分内容（tag 打在 tip 上，两节共用同一区间）。

### 新增

- 智能问答页新增 LLM 模型选择器（可按会话临时切模型）
- Wiki 支持「模式切换时首次建页」与建页状态 / 阶段进度展示

### 修复

- Wiki：记录来源被删除后仍能打开悬空页面的问题（改为按 recorded source 过滤）
- Wiki：首次建库期间删除文档导致的状态错乱
- Wiki：状态轮询与进度写入的并发问题
- 问答：模型切换评审发现的若干问题
- 桌面：模型配置不再出现在桌面端日志里

### 样式

- 内容区空白背景改为白色点阵背景
- 长会话时底部输入框固定在视口底部；长会话滚动时左侧会话列表保持可见，会话区改圆角描边
- 模型设置页三类模型改成分组卡片，去掉页签卡片多余的竖向滚动条

---

## [1.0.16] - 2026-11-07

### 新增

- 会话列表支持置顶，并提升条目辨识度（卡片式条目、时间与消息数）
- 模型列表按用途分组折叠

### 修复

- 个人 Agent 的模型展示名同时显示模型标识，避免同名模型分不清来源
- 离线回复在界面上说明原因，并优先使用已验证模型
- 精简各页面多余的小字说明
- 补齐页签条与表格表头缺失的圆角

---

## [1.0.15] - 2026-11-07

### 修复

- 个人 Agent 页面卡在「正在加载 Agent 核心」
- 检索卡死，以及重建索引后知识库仍显示「索引中」

---

## [1.0.14] - 2026-11-07

### 新增

- 个人 Agent：可更换核心，支持 CLI 插件体系（内置核心 / 插件来源、安装探测、启停、插件目录）
- 问答：实现检索链路，并在回答中流式展示思考过程

### 文档

- 说明密钥应放在环境变量而不是命令行参数
- 视觉证据标注基线与改动提交

---

## [1.0.13] - 2026-11-06

### 修复

- Wiki：表格块在章节切分时整块保留；离线 Wiki 正文保留表格换行
- UI：状态点用形状区分「待校对」，修正消息图标裁切
- 时间：统一按本地时区显示，修正向量模式文案与时间契约

### 样式

- 统一线性图标与信息层级

---

## [1.0.12] - 2026-11-06

### 新增

- 用量统计看板（调用次数与成本归因）
- 直接使用供应商预设配置

### 修复

- 会话抽屉的键盘可达性；会话导航与输入体验
- 模型设置页三个用途的表格列宽对齐，「启用」开关显示不完整

---

## [1.0.11] - 2026-11-04

### 修复

- 离开问答页不再丢掉正在生成的回答（提问先落库 + pending 轮询）
- 知识库页「卡片 / 表格」切换控件重做
- README 拼写与 Markdown 结构错误

---

## [1.0.10] - 2026-11-01

### 修复

- **任务处理中整个软件卡死**：事件循环在请求路径上同步 `Thread.start()`，被 Windows loader lock 拖住。
  改为常驻 HTTP 线程池 + 短写事务 + 去 numpy 打包 + 启动预热（详见 [`docs/DESIGN.md`](docs/DESIGN.md) §4.5）
- 安装 / 退出治理：按进程树结束 sidecar、后端单实例守护、SQLite 写锁分级与看门狗
- 弹窗输入框每敲一个字就丢焦点

### 新增

- 前端任务心跳、写锁横幅、关于页「后端诊断 / 一键重启后端」
- 自动更新多源回落（国内加速源 + GitHub 兜底）

### 样式

- 前端视觉重做：设计系统 + 全站页面 + 登录 / 问答 / 知识库详情重构

---

## [1.0.9] - 2026-10-28

- 打包并发布 GitHub Release，实测自动更新检出（安装包 / `.blockmap` / `latest.yml` 三个资产）
- 修复 `no-orphan.ps1` 的两个假红坑（`ELECTRON_RUN_AS_NODE` 干扰、未等后端就绪就强杀）
- 出处：看板归档卡「v1.0.9 发布：打包 + GitHub Release + 自动更新检出实测」（[`docs/archive/dsh-kanban-summary.md`](docs/archive/dsh-kanban-summary.md)）

## [1.0.2] ~ [1.0.8] - 2026-10-28 ~ 2026-10-31

> 这 7 个 tag 指向同一个提交，逐版本差异无法从 git 还原。以下里程碑可核对：

- **安装向导中文化**（NSIS `multiLanguageInstaller` 配置修复）
- **孤儿进程专项**：Electron 按进程树结束 sidecar、后端父进程死亡自毁 + 数据目录文件锁、
  SQLite 写锁分级 `busy_timeout` 与写重试、写锁看门狗与诊断接口、对应的回归测试
  （出处：看板归档的「修复 A ~ 修复 F」6 张卡）
- **安装包无法关闭运行中的应用**（NSIS `customCheckAppRunning` 改成先礼貌退出再强杀）
- **每次启动弹「应用启动失败 ReferenceError: feed is not defined」**
- 自动更新链路实测：1.0.1 → 1.0.2 全量、1.0.2 → 1.0.3 差量（见 [`docs/UPDATE.md`](docs/UPDATE.md) §2.1）

## [1.0.1] - 2026-10-28

- 自动更新：接入 `electron-updater`，支持 GitHub Releases 与自建通用源；关于页新增更新卡片
- 出处：看板归档卡「自动更新：electron-updater + 关于页更新卡片 + v1.0.1」

## [1.0.0] - 2026-10-28

首个可交付版本，包含：

- 后端骨架（配置、ORM 模型、鉴权、依赖注入）与 `SQLite + WAL`
- 模型调用层（OpenAI / Anthropic / 自定义兼容协议，含离线降级）
- 文档解析与分块（多格式、位置信息、关键词；表格块按字符预算切分）
- 检索层（关键词 + 向量加权 RRF，Wiki 章节检索，跨库路由）
- Wiki 生成与后台任务（Map-Reduce 分片归纳、证据回挂、幂等与重试）
- API 路由层（认证 / 知识库 / 文档 / 任务 / Wiki / 会话 / 设置）
- 前端七个页面 + API 客户端 + 权限路由
- 测试（权限回归、检索融合、路由回退、幂等、端到端）
- 种子数据、演示资料、一键启动脚本
- 文档：README、设计说明、论文与实验方案
- 桌面化：Electron 外壳 + Python sidecar 生命周期管理

> 出处：看板归档的首批 12 张开发卡（[`docs/archive/dsh-kanban-summary.md`](docs/archive/dsh-kanban-summary.md)）。

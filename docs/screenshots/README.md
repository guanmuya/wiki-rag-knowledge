# 界面图（`docs/screenshots/`）

本目录只放**当前版本的界面图**，供 `docs/THESIS.md`、`docs/DESIGN.md` 正文引用。
历史 before/after 对比图与旧版整站截图已移到 [`docs/archive/screenshots/`](../archive/screenshots/README.md)。

## 1. 这批图是怎么来的

| 项 | 值 |
| --- | --- |
| 拍摄时间 | 2026-11-11 |
| 对应版本 | `1.0.17`（`backend/app/main.py` 的 `VERSION`、`frontend/package.json` 的 `version`） |
| 数据 | `backend/scripts/seed_demo.py` 灌的 3 个演示知识库（人事制度库 / 客户支持库 / 产品与销售库） |
| 环境 | 本机开发实例：`uvicorn` 后端 + Vite 前端（`127.0.0.1`），Chromium 无头浏览器 |
| 视口 | 1440×900，`zh-CN`，浅色主题，浏览器缩放 100% |
| 模型 | **离线配置**（未接在线模型）：回答走离线抽取，页面上会写明「离线演示模式」，因此图里不含任何真实密钥或私有内容 |
| 生成方式 | `scripts/dev-only/capture-ui.mjs`（一次性脚本，不进 CI）；本次实跑 15 张、0 个前端运行时错误 |

复现命令（三个终端分别起后端、Vite，再拍照）：

```powershell
# 终端 1：后端（仓库根目录）
$env:PYTHONUTF8=1; python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000

# 终端 2：前端（frontend/ 目录）
node node_modules/vite/bin/vite.js --port 5173 --host 127.0.0.1

# 终端 3：拍照（frontend/ 目录，playwright 从这里解析）
$env:UI_PASS='<管理员口令>'; node ../scripts/dev-only/capture-ui.mjs
```

> 口令请临时设置，不要写进任何文件或提交信息。

## 2. 图目录（正文引用用这些路径）

| 文件 | 画面 | 适合放进 |
| --- | --- | --- |
| `current/01-login.png` | 登录页：账号口令登录、左侧能力概览 | 设计说明「首次使用」、答辩开场 |
| `current/02-kb-list.png` | 知识库列表：三个库的模式、索引状态、文档/分块/Wiki 页数 | 设计说明「知识库形态」 |
| `current/03-kb-documents.png` | 知识库详情·文档页签：文档版本与入库状态 | 流水线章节 |
| `current/04-kb-wiki.png` | 知识库详情·Wiki 页签：页面目录 + 已发布页面正文（表格块） | Wiki 生成、表格分块章节 |
| `current/05-kb-wiki-page.png` | Wiki 页面「来源依据」：正文与它回挂的证据片段、关联页面 | 证据链、Wiki 一致性章节 |
| `current/06-kb-settings.png` | 知识库设置：检索模式、索引状态、重建入口、最近任务 | 索引版本与重建章节 |
| `current/07-chat-empty.png` | 智能问答空态：模型选择、知识库多选、底部固定输入框 | 交互设计、问答入口 |
| `current/08-chat-answer.png` | 一轮完整问答：思考过程、引用编号 `[1]..[4]`、证据 9 条、路由 3 个库 | 检索融合、证据链与拒答章节 |
| `current/09-search.png` | 检索调试：检索概览（候选/入选/覆盖率/RRF k）、逐库路由与决策原因、证据列表 | 跨库路由、检索融合章节（最有信息量的一张） |
| `current/10-tasks.png` | 任务中心：任务阶段、进度与失败语义 | 任务队列与失败语义章节 |
| `current/11-users.png` | 用户管理：账号、角色、库级授权 | 权限模型章节 |
| `current/12-models.png` | 模型设置：按用途分组的三种协议配置（离线态无真实模型条目） | 模型协议适配章节 |
| `current/13-usage.png` | 用量概览：调用次数与成本归因 | 成本核算章节 |
| `current/14-about.png` | 关于页：版本、后端诊断、更新卡片 | 交付与运维章节 |
| `current/15-agent.png` | 个人 Agent：核心与插件、运行环境探测 | 个人 Agent 章节 |

## 3. 正文引用约定

- 引用写法：`![图 N：一句话说明](screenshots/current/09-search.png)`（`docs/*.md` 里相对 `docs/` 解析）
- **一张图只说明一件事**，说明写在图注里，正文紧接着讲结论，不要让读者自己找亮点
- 图里出现的数字（例如「候选 4 / 入选 4 / 平均覆盖率 0.20」）是**这次拍摄时**的读数，
  写进论文时要么注明「一次实测」，要么改成「见结果文件」的量；需要长期引用的数值仍以
  [`docs/FACTS.md`](../FACTS.md) 和 [`docs/results/`](../results/README.md) 为准

## 4. 体量与维护

- 当前 15 张 PNG，合计约 1.2 MB（1440×900，单张 55–122 KB）
- 换版后**重拍覆盖**同名文件即可，不要靠追加新文件堆积历史；旧图要留就进 `docs/archive/screenshots/history/`
- 截图前先确认页面上没有真实密钥、真实姓名、私有文档内容；演示数据用 `seed_demo.py` 生成的最安全

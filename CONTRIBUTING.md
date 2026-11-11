# 参与开发（CONTRIBUTING）

本文是这个仓库的**最小协作约定**：怎么装环境、改完要跑什么、提交怎么写、哪些东西不能进仓库。
面向「接手这份代码继续改」的人，也面向未来的自己。

> 交付形态是 **Windows 桌面程序**（Electron + Python sidecar），所以下面默认 Windows 环境；
> 命令都是 PowerShell / bash 通用写法。

---

## 1. 环境准备

依赖版本 **不写在文档里，写在锁文件里**：后端 `backend/requirements*.txt` + `backend/constraints.txt`，
前端 `frontend/pnpm-lock.yaml`。CI 与本机用的是同一份。

```powershell
# 后端：Python 3.13
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r backend/requirements-dev.txt -c backend/constraints.txt

# 前端：Node 22+、pnpm 9
cd frontend
pnpm install --frozen-lockfile
```

`.venv/` 与 `node_modules/` 都在 `.gitignore` 里，不要提交。

## 2. 改完必须跑的五个门禁

与 `.github/workflows/ci.yml` **完全一致**，任意一项失败即不允许合并：

```powershell
# ① 后端全量测试
python -m pytest backend/tests -q

# ② 前后端契约一致性
cd frontend; pnpm check:contract

# ③ 前端类型检查
cd frontend; pnpm typecheck

# ④ 前端 Web 构建
cd frontend; pnpm build:web

# ⑤ 前端自动化测试
cd frontend; pnpm test
```

测试**确实通过的输出**才叫证据。数字口径（目前 673 passed / 1 skipped 的来源、命令、环境、skip 原因）
只在 [`docs/FACTS.md`](docs/FACTS.md) §2 维护一处，**改完顺手更新那一处，不要在别的文档里另写一份**。

## 3. 提交信息

格式：`type(scope): 一句话`，`scope` 可省。

- `type` 只用：`feat` `fix` `refactor` `perf` `docs` `test` `build` `ci` `chore` `style` `revert`
- 一句话说**做了什么**，不要「update files」这种没有信息量的摘要
- 需要背景时，空一行再写正文（为什么改、影响范围、验证方式）
- **不要**在提交信息里加 `Co-Authored-By` 或任何「由 AI 生成」的署名

```
fix(wiki): hide pages when recorded sources are deleted

删除来源后再打开 Wiki 会读到悬空证据，改为按 recorded source 过滤。
验证：python -m pytest backend/tests -q（全绿）
```

## 4. 分支与合并

- 主分支 `master`；功能/修复都从 `master` 切分支，**通过 PR 合回**
- PR 必须等 CI 五项全绿再合并；合并用普通 merge，不要 force push 到 `master`
- 一个 PR 只做一件事：跨后端的改动尽量拆分，方便回滚
- 版本号在 `frontend/package.json` 与 `backend/app/main.py` 的 `VERSION` 两处联动，
  发版流程见 [`docs/UPDATE.md`](docs/UPDATE.md)

## 5. 目录与归档约定

| 位置 | 放什么 | 不放什么 |
| --- | --- | --- |
| `backend/app/` | 产品后端代码 | 一次性脚本、临时数据 |
| `frontend/src/` | 产品前端代码 | 验收脚本、截图 |
| `frontend/scripts/` | 构建/开发入口 + 人工验收辅助 | 自动化测试（在 `frontend/tests/`） |
| `scripts/` | **长期维护**的入口脚本（`dev.sh` / `start_backend.sh` / `start_frontend.sh` / `make_fixtures.py` / `make_icon.py`） | 一次性排障脚本 |
| `scripts/dev-only/` | 一次性排障与人工验收脚本（不进 CI、不属于交付物） | 构建链路依赖的脚本 |
| `docs/` | 设计、论文、实验、接口文档 | 大文件二进制素材 |
| `docs/screenshots/current/` | **当前版本**的界面图（供论文/设计说明引用） | 历史对比图 |
| `docs/archive/` | 历史档案：旧看板、历史交接快照、历史 before/after 截图 | 任何「当前事实」 |

新增一次性脚本请直接放 `scripts/dev-only/`，并在文件头用一行说明「仅开发排障用」；
新增截图请放 `docs/screenshots/current/` 并在 `docs/screenshots/README.md` 登记。
历史材料进 `docs/archive/` 时，同步在 `docs/archive/README.md` 写明来龙去脉。

## 6. 文档口径

- **代码是唯一事实源**：版本号、数据表数、并发数、锁超时、测试基线都从代码里读
- 数值只写在 [`docs/FACTS.md`](docs/FACTS.md)，其它文档放链接，不复述
- `docs/EXPERIMENT.md` 有一条硬约定：**凡未能实测的一律标注「待实验」，禁止填推测数字**
- 引用代码位置优先写「文件 + 符号名/章节」，少写易漂移的 `文件:行`

## 7. 不要提交这些

已在 `.gitignore` 中，请勿用 `git add -f` 绕过：

- 运行时数据：`data/`、`*.sqlite3*`、`backend/uploads/`、`secrets/`
- 依赖与构建产物：`.venv/`、`node_modules/`、`frontend/dist/`、`frontend/release/`、`frontend/build/`
- 本地临时物：`.tmp-*` 目录与文件、`*.log`、`.pytest_cache/`、`__pycache__/`
- 私钥与证书：`*.key`、`*.pem`

## 8. 密钥与凭据

- 模型 API Key、初始管理员口令等**只能**通过环境变量或应用内的设置界面录入
- 不要把真实凭据写进代码、文档、截图、提交信息，也不要贴到聊天/工单里
- 测试与演示一律用临时生成的口令；需要举例时写占位符（如 `<你的初始密码>`）
- 发现仓库里已经存在凭据时，先按「轮换凭据 → 再清理仓库」的顺序处理，别只删文件

## 9. 报问题

带三样东西最省事：**复现步骤**、**期望 vs 实际**、**日志位置**
（桌面版日志在 `%APPDATA%\wiki-rag-desktop\logs\`，开发态在仓库 `data/logs/`）。
界面问题附一张 `docs/screenshots/current/` 口径的截图。

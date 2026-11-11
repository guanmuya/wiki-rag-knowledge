# `scripts/dev-only/` — 一次性排障与人工验收脚本

**这里的脚本不是交付物，不参与构建，不接入 CI，也不保证跟着代码一起演进。**
它们记录的是「某一次问题是怎么定位的」「某个界面改动当时怎么验收的」。

放在这里而不是直接删掉的理由：这些脚本里有大量具体的排查路径（看哪张表、盯哪个锁、
用什么断言判红），是排障线索。但它们的定位始终是**人工辅助**：

- 没有统一运行器、没有统一退出码约定；
- 多个脚本需要你先手工起 Vite / 后端 / 打包好的 exe；
- 部分脚本写于路由方式、目录结构调整之前，**可能已经跑不通**（见下表「状态」列）。

> 需要「可重复运行、失败即阻断合并」的自动化测试在 `frontend/tests/` 与
> `backend/tests/`；两者都已纳入 CI。别把这些脚本写进 `pnpm test`。

## 1. 清单

### 1.1 仍在使用的

| 脚本 | 做什么 | 状态 |
| --- | --- | --- |
| `capture-ui.mjs` | 拍当前版本界面图 → `docs/screenshots/current/`（15 张，见该目录 README） | 可用（2026-11-11 实跑通过） |
| `diag/lock_probe.py` | 写锁探针：**只读复制**一份库到 `.tmp-diag/`，排任务并盯 `database is locked`，可 dump 持锁线程栈 | 可用 |
| `verify/no-orphan.ps1` | 排查「退出应用后还有 `wrk-backend.exe` 在跑」：按进程树、命令行、端口逐项核对 | 可用（需打包产物） |
| `verify/freeze_check.py` | 打包后的后端 exe 是否还会「任务处理中整机卡死」的回归检查 | 可用（需 `frontend/build/backend/wrk-backend.exe`） |
| `verify/smoke_backend_exe.py` | 打包后端 exe 的冒烟：能起来、能应答、能干净退出 | 可用（同上） |
| `verify/e2e-setup.mjs` | 打包版（Electron 窗口）首次启动引导的端到端验收：建号 → 弱口令拦截 → 登录 | 可用（需 `frontend/release/win-unpacked/`） |
| `live_check.py` | 对**正在运行的实例**做一轮真实 HTTP 验收（不依赖 TestClient），口令取 `WRK_ADMIN_PASSWORD` | 可用 |
| `check_questions.py` | 抽查 `demo_data/questions.json` 的检索命中（打 `/api/search/preview` 判断 answer_keywords） | 可用，口令取 `WRK_ADMIN_PASSWORD` |
| `inspect_db.py` | 只读打印某个 sqlite 库的关键表内容 | 可用（传库路径） |
| `build_opening_report.py` | 把 `docs/开题报告.md` 转成可提交的 `.docx`（需要 python-docx） | 视文档是否还在而定 |

### 1.2 历史脚本（可能已跑不通，仅供查线索）

| 脚本 | 做什么 | 状态 |
| --- | --- | --- |
| `ui_check.mjs` | 老的全站验收：登录 → 各页面截图 → 提问 → 检查证据与路由 | **已过时**：项目改用 `HashRouter` 后路由是 `/#/kbs`，脚本里的 `/kbs` 路径不再成立；截图输出已改到 `.tmp-verify/shots/`。要看当前界面请用 `capture-ui.mjs` |
| `legacy/frontend-ui_check.mjs` | 上面那个脚本当年误放 `frontend/` 根目录的**原样副本**（与原始文件字节相同） | 仅留档；属于「可删候选」，删除需维护者确认 |
| `diag/prove_old_freeze.py` | 把补丁还原成旧实现，证明回归测试确实能抓到旧行为 | 依赖当时的 anyio 内部结构，可能跑不通 |
| `diag/repro_freeze_exe.py` | 直接驱动打包后的后端 exe 复现「整机卡死」，默认找 `frontend/build/backend/wrk-backend.exe`（可用 `WRK_BACKEND_EXE` 覆盖） | 需打包产物 |
| `verify/shot-installer.ps1` | 截 NSIS 安装包窗口，用来确认安装向导语言 | 需安装包 |

## 2. 使用纪律

1. **不影响仓库状态**：临时文件落 `.tmp-*`、`.tmp-verify/`（已 gitignore）；
   脚本不再往 `docs/` 写图片。
2. **不碰真实数据**：`lock_probe.py` 只读复制库；任何要连真实 `%APPDATA%` 数据目录的操作，
   先在副本上验证。
3. **不内置口令**：一律读环境变量（`WRK_ADMIN_PASSWORD` 等），拿不到就报错退出；
   不要把真实口令写回脚本。
4. **路径按脚本自身定位**：用 `Path(__file__).resolve().parents[N]` / `$PSScriptRoot` /
   `import.meta.dirname`，不要写死盘符或用户名。
5. 新增脚本请在本表登记一行（脚本 / 做什么 / 状态），并在文件头写「仅开发排障用」。

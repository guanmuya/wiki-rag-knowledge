# `scripts/` 目录说明

本目录只放**长期维护的入口脚本**：会被 README、`package.json`、CI 或测试引用的那几个。
一次性排障 / 人工验收脚本统一收在 [`dev-only/`](dev-only/README.md)。

## 1. 长期维护的入口（改之前先想想谁在用）

| 脚本 | 用途 | 被谁引用 |
| --- | --- | --- |
| `dev.sh` | 一键开发：建 venv、装依赖、灌演示数据、同时起前后端 | `README.md` §3 |
| `start_backend.sh` | 只起后端（`uvicorn`，默认 `127.0.0.1:8000`） | `README.md` §3 / §10 |
| `start_frontend.sh` | 只起前端 Vite（5173，`/api` 代理到 8000） | `README.md` §3 |
| `make_fixtures.py` | 生成表格分块测试夹具到 `demo_data/fixtures/` | `README.md` §9、`backend/tests/test_tables.py` |
| `make_icon.py` | 生成 `frontend/build/icon.ico` | 打包说明（图标可选） |

这五个是「新克隆者照 README 就能跑通」的一部分，改动要和文档同步。

## 2. 一次性脚本（`dev-only/`）

排障探针、截图脚本、安装包验收脚本都在 `dev-only/`，**不进 CI、不属于交付物、不需要保持可跑**。
详细清单与各自用途见 [`dev-only/README.md`](dev-only/README.md)。

## 3. 约定

- 新写一个脚本前先问一句：**它会被 CI 或测试调用吗？**
  - 会 → 放本目录根下，并在上表登记；
  - 不会（只是这次排查问题用）→ 直接放 `dev-only/`，文件头写一行「仅开发排障用」。
- 脚本里**不要写死本机绝对路径**：用 `Path(__file__).resolve().parents[N]`、
  `$PSScriptRoot` 或环境变量（`WRK_*` / `UI_*`）来定位；
- 脚本里**不要内置口令与密钥**：用环境变量（如 `WRK_ADMIN_PASSWORD`），拿不到就报错退出；
- 脚本产生的临时文件落到 `.tmp-*` 或 `.tmp-verify/`（已在 `.gitignore`），不要写进 `docs/`。

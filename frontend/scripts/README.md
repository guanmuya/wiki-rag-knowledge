# `frontend/scripts/` 脚本说明

本目录混放了两类脚本，用途不同，请勿混为一谈。

## 1. 长期维护的构建 / 开发入口

| 脚本 | 用途 |
| --- | --- |
| `dev.mjs` | 开发模式：Vite + Electron 窗口 |
| `build.mjs` | 打包编排（前端构建 + 后端 exe + electron-builder） |
| `build-backend.mjs` | 用锁定版本的 PyInstaller 打后端 exe（P1-2） |

这三个是产品构建链路的一部分，由 `package.json` 的 `dev` / `build` / `pack` 调用。

## 2. `verify-*` 是人工验收辅助，**不是自动化测试**

下面 11 个 `verify-*` 脚本是开发过程中针对具体 UI 回归写的**一次性验收辅助脚本**：

- `verify-chat-composer-dock.mjs`
- `verify-chat-layout.mjs`
- `verify-chat-model-selector.mjs`
- `verify-chat-navigation.mjs`
- `verify-modal-input.mjs`
- `verify-model-collapse.mjs`
- `verify-model-status.mjs`
- `verify-setup-flow.cjs`
- `verify-stop-wiki.mjs`
- `verify-ui-workflows.cjs`
- `verify-wiki-mode-switch.cjs`

它们的共同特点：**需要人工先起 Vite（部分需设 `UI_BASE` / `CHAT_TEST_BASE_URL` 等环境变量）、
人工看截图或控制台输出来判断结果**，脚本本身没有统一运行器、没有统一退出码约定，
也**没有接入 CI、不参与 `pnpm test`、不作为合并门禁**。

因此它们的定位是「**人工验收时的辅助工具**」，不是测试套件。
仓库根目录 `scripts/verify/` 下的 5 个专项脚本
（`e2e-setup.mjs`、`freeze_check.py`、`no-orphan.ps1`、`shot-installer.ps1`、
`smoke_backend_exe.py`）同理，属于打包 / 安装包的一次性排障与验收辅助。

真正可重复运行、纳入 CI 的自动化测试在 **`frontend/tests/`**，运行命令是：

```bash
cd frontend && pnpm test
```

详见 `frontend/tests/README.md`。

> 说明：保留这些脚本是因为它们记录了大量具体 UI 场景（密码强度、会话竞态、
> 移动端抽屉等）的历史验收证据，删除会丢失排障线索；但不要再给它们
> 补「跑通就算测试通过」的期待，也不要把它们写进 CI。

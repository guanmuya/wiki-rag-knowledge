# 模型展示名带上模型标识 · 视觉证据

同一台机器、同一套演示数据（本地临时创建：1 个供应商 + 3 个显示名相同的对话模型 + 1 个快速模型）、
1440×900 浅色主题、Chrome 无头浏览器。基线为 `master` 的 `a24d9f7`（独立 worktree 检出），
改动为分支提交 `fbc1581`。

| 文件 | 版本 | 场景 |
| --- | --- | --- |
| `before-agent-model-open.png` | 基线 `a24d9f7` | 个人 Agent 顶部「模型」下拉：三个 `演示对话模型 · 演示供应商` 完全一样，分不清是哪个渠道 |
| `after-agent-model-open.png` | 改动 `fbc1581` | 同一会话、同一选中项、同一展开位置：`演示对话模型 · vendor-a/demo-chat · 演示供应商`、`… vendor-b …`、`… vendor-c …` |
| `before-agent-toolbar.png` | 基线 `a24d9f7` | 收起态：顶部选择器、左侧核心状态、底部提示都只有 `显示名 · 供应商` |
| `after-agent-toolbar.png` | 改动 `fbc1581` | 同一位置显示 `显示名 · 模型标识 · 供应商` |
| `context-models-page.png` | 两版一致 | 模型设置页（用户截图所在页）：显示名下方本来就显示模型标识，本次未改 |

说明：

- 截图用的是本地开发实例（`uvicorn` + Vite）和演示数据；模型指向 `https://example.invalid/v1`，
  截图过程没有发送消息，不产生外部模型调用。
- 基线截图来自独立的 `git worktree`（`a24d9f7`），不是把改动隐藏后补拍的。
- 只改了后端 `model_label()`，前端无改动；模型设置页因此前后一致。

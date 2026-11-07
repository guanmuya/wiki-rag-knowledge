# 智能问答页 LLM 模型选择器 · 视觉证据

同一台机器、同一套演示数据、1440×900 浅色主题、Chromium 无头浏览器；两版都用
同一份 mock `/api` 响应（1 个知识库、2 条会话、3 个对话模型），停在智能问答页的
空会话状态。基线为 `master` 的 `92f9618`（独立 worktree 检出），改动为分支
`agent/chat-model-selector-759547b02b16` 的提交（本 PR head）。

| 文件 | 版本 | 场景 |
| --- | --- | --- |
| `before-chat-toolbar.png` | 基线 `92f9618` | 智能问答页：工具栏右侧只有「清空选择」，没有模型选择器 |
| `after-chat-toolbar.png` | 改动分支 | 同一页、同一视口：右侧新增「模型」下拉，默认显示设置好的 `GPT-4o mini · gpt-4o-mini · OpenAI（默认）` |
| `before-toolbar-detail.png` / `after-toolbar-detail.png` | 同上 | 工具栏裁切，便于逐处对比 |
| `after-model-switched.png` | 改动分支 | 同一页把模型切到 `DeepSeek-V3 · deepseek-chat · DeepSeek`，选择器可临时改选 |
| `after-switched-toolbar-detail.png` | 改动分支 | 切换后的工具栏裁切 |

说明：

- 基线截图来自独立 `git worktree`（`master` 的 `92f9618`），不是把新控件隐藏后补拍；
  两版分别由各自源码目录启动的 Vite 开发实例提供页面。
- `/api` 请求全部在浏览器层拦截并注入演示数据，未连接真实后端、未发送消息、
  未产生任何模型调用；截图中不含有真实配置、密钥或私有内容。
- 下拉默认项由后端 `llm.resolve_model()` 解析出的 `default_id` 决定，与未指定
  模型时问答实际使用的模型一致；标「（默认）」的那条即为设置好的模型。
- 采集脚本同时断言：基线无选择器、改动版默认选中 `default_id`、可切换到另一模型，
  且改选后的 `model_config_id` 会随 `/ask/stream` 请求一起提交。

# 底部输入框固定：视觉证据

同一台机器、同一份模拟长会话（12 条消息）、浅色主题、Chromium 无头浏览器；
桌面 1440×900、移动 390×844。两次截图都把会话滚到相同位置，页面本身不滚动。

| 文件 | 版本 | 场景 |
| --- | --- | --- |
| `before-top.png` | 基线 `92f9618`（独立 worktree） | 长会话顶部：整页高 1889px，输入框在 y=1764–1889，视口内完全看不到 |
| `after-top.png` | 本次改动 | 同一位置：页面高 900px，输入框停靠 y=775–900，消息区自行滚动 |
| `before-bottom.png` | 基线 `92f9618` | 尝试滚到底部：消息区 `clientHeight == scrollHeight`，无内部滚动，输入框仍在视口外 |
| `after-bottom.png` | 本次改动 | 同一位置：消息区滚到底，输入框仍固定在视口底部 |
| `after-mobile-top.png` / `after-mobile-bottom.png` | 本次改动 | 390×844：输入框停靠 y=698–844，同样固定在底部 |

关键量测（脚本运行时另会在输出目录写一份 `*-metrics.json` 原始记录，不入库）：

| 指标 | 基线 `92f9618` | 改动后（1440×900） | 改动后（390×844） |
| --- | --- | --- | --- |
| 文档高度 / 视口高度 | 1889 / 900 | 900 / 900 | 844 / 844 |
| 输入框位置（top–bottom） | 1764–1889（视口外） | 775–900 | 698–844 |
| 消息区 clientHeight / scrollHeight | 1658 / 1658（不滚动） | 669 / 1658 | 592 / 2667 |
| 输入框固定在底部 | 否 | 是 | 是 |

复现方式（先分别启动两个 Vite）：

```bash
pnpm exec vite --host 127.0.0.1 --port 15273      # 改动后的工作树
pnpm exec vite --host 127.0.0.1 --port 15274      # 92f9618 的 worktree
node scripts/verify-chat-composer-dock.mjs --label=before           # CHAT_DOCK_BASE_URL=...:15274
node scripts/verify-chat-composer-dock.mjs --label=after            # 默认 15273，带回归断言
CHAT_DOCK_VIEWPORT=390x844 node scripts/verify-chat-composer-dock.mjs --label=after-mobile
```

脚本在浏览器层拦截所有 `/api` 请求，不连接真实后端、不含任何真实业务数据。

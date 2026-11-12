# 前端自动化测试

体检清单 `docs/HEALTH_CHECK.md` §4.2 **P1-3「给前端补真实自动化测试」** 的产出。

本目录是**唯一纳入 CI 的前端测试**。
`frontend/scripts/verify-*` 与 `scripts/dev-only/` 里的脚本是一次性人工验收辅助，
不是自动化测试，也不参与合并门禁（见 `frontend/scripts/README.md` 与 `scripts/dev-only/README.md`）。

---

## 1. 怎么跑

```bash
cd frontend
pnpm test
```

`pnpm test` 即 `node --test tests/e2e.test.mjs tests/error-hints.test.mjs tests/agent-no-kb.test.mjs tests/usage-page.test.mjs tests/ui-controls.test.mjs tests/theme.test.mjs`，
把六个测试文件都跑一遍（新增测试文件记得同步写进这个脚本）：

| 文件 | 内容 | 用例数 |
| --- | --- | --- |
| `tests/e2e.test.mjs` | 真实 Chromium + Vite 的主链路 E2E（后端换成确定性 mock） | 5 |
| `tests/error-hints.test.mjs` | 纯 Node，不需要浏览器：失败文案单一来源的回归（体检清单 P2-6） | 5 |
| `tests/agent-no-kb.test.mjs` | 纯 Node 静态扫描：个人 Agent 不再接知识库的回归 | 5 |
| `tests/usage-page.test.mjs` | 真实 Chromium + Vite：管理员用量页（六张指标卡、趋势图切换、未计价提示、单价面板） | 4 |
| `tests/ui-controls.test.mjs` | 纯 Node 静态扫描：复选框只做本体、下拉框统一走 `Select`、箭头不写死颜色 | 4 |
| `tests/theme.test.mjs` | 纯 Node + 真实 Chromium：深色 / 浅色 / 跟随系统外观（令牌、首屏防闪、持久化、实时跟随、主进程偏好、箭头只画一个） | 12 |

CI（`.github/workflows/ci.yml`）在 `windows-latest` 上跑同一条命令，失败即阻断合并。

前端另外还有一项**不在这里**的 CI 检查：`pnpm check:contract`
（`frontend/scripts/check-api-contract.mjs`，前后端契约一致性）。
它不是浏览器测试，只静态比对 `types.ts` / `docs/API.md` 与后端模型，见 `docs/API.md` §10。

不需要先启动后端，也不需要手工起 Vite：测试进程内用 `vite.createServer`
在固定端口 `15173`（`strictPort: true`）启动 dev server，测试结束后自动关闭；
可用环境变量 `E2E_PORT` 改端口。`tests/usage-page.test.mjs` 用另一个端口 `15174`
（`USAGE_E2E_PORT` 可改）、`tests/theme.test.mjs` 用 `15175`（`THEME_E2E_PORT` 可改），
各自带独立的 Vite 缓存目录，因为 `node --test` 会并行跑这些文件。
需要本机装有 Playwright 的 Chromium：

```bash
cd frontend
pnpm exec playwright install chromium
```

运行器用 Node 内置的 `node:test`，浏览器用项目已有的 `playwright` 核心包，
**没有新增任何 npm 依赖**，因此 `pnpm-lock.yaml` 不变、CI 的 `--frozen-lockfile` 不受影响。

---

## 2. 离线模式下如何保证可重复

体检清单的开放问题是「端到端用例在离线模式下如何保证可重复」。
按现有 `verify-*` 脚本验证过的模式，答案是**把后端换成确定性的内存 mock**，
而不是去固定语料、固定随机性或起真实 sidecar：

- 所有 `/api/**` 请求由 `page.route` 在浏览器网络层拦截，用同一份内存数据响应。
  测试**不启动 FastAPI sidecar**，不需要在线模型 / API Key，不需要灌 `demo_data` 语料，
  也不依赖 SQLite 或向量索引。
- 回答文本、时间戳、消息 ID、token 全部写死；**没有任何随机数或真实模型推理**，
  所以耗时与断言结果逐次一致。
- 每个用例一个独立 `browser.newContext()`，localStorage 与路由状态互不污染；
  固定端口 + `strictPort` 避免端口漂移。
- 覆盖的是**真实 Chromium + 真实 React 前端 + 真实 Vite 编译**，只有 HTTP 后端被替换，
  因此仍是端到端用例，不是组件快照或纯函数断言。

这也意味着它验证的是前端链路（路由、鉴权头、表单、列表刷新、SSE 流式渲染），
**不替代后端 `python -m pytest backend/tests -q`**，两者在 CI 里并列运行。

---

## 3. 用例清单

| 用例 | 断言的真实行为 |
| --- | --- |
| 未登录访问业务页被送回登录页 | `RequireAuth` 重定向到 `#/login`，期间没有发出登录或问答请求 |
| 带有效令牌访问业务页：恢复登录态，不被送回登录页 | `AuthProvider` 用 `/api/auth/me` 恢复登录态，`#/kbs` 不被重定向、管理员可见「新建知识库」、期间没有重新登录请求 |
| 登录凭据错误：停留在登录页并显示服务端错误 | 401 后 `.login-error` 展示后端 detail、URL 不变、登录请求确实发出且用户名正确 |
| 登录 → 新建知识库 → 智能问答收到回答 | 登录后跳 `#/kbs`；建库请求真的发出且列表刷新出新库；提问走 `/ask/stream` 且带 `Bearer` token、问题文本正确；确定性回答渲染在 `.chat-thread` |
| 页面渲染异常时显示兜底页而不是白屏 | mock 持续下发结构损坏的知识库（`name` 是对象），真实 React 渲染时真的抛错；断言出现 `ErrorBoundary` 兜底页（标题「界面出了点问题」+ 重试/回到首页入口）、`#root` 不是空串；随后把 mock 恢复正常并点「回到首页」，断言 boundary 复位、列表重新渲染（体检清单 P1-10 的验收项） |

主链路用例覆盖体检清单建议的「登录 → 建库 → 上传 → 问答」中的
**登录 → 建库 → 问答** 部分。上传依赖 multipart 文件选择与入库任务，
当前离线 mock 不模拟入库流水线，暂未纳入自动化用例；
这一段的验收仍由 `frontend/scripts/verify-*` 人工辅助完成。

错误兜底用例只触发**渲染期**异常；事件处理器与异步回调里的错误不走 React 错误边界，
由各页面的 `ApiError` / `ErrorState` 处理，见 `docs/API.md` §9.2。

---

## 4. 加新用例的约定

- 新接口一律加进 `createMock()` 的 `handle()`，未知接口会返回 `200 {}` 并被记进
  `state.unknown`，写断言前先看它是否被意外命中。
- 每个用例自己 `openWithMock()`，用完 `context.close()`。
- 断言 UI 用 `waitFor()` 轮询，不要拿一次快照判生死。
- 不要在测试里引入真实时间、随机数或外部服务，保持「离线可重复」这条底线。
- 不依赖浏览器的用例（纯数据/纯函数/静态扫描）另开一个 `*.test.mjs`，不要塞进
  `e2e.test.mjs`：那个文件的用例数（5 条）是发布口径里的「E2E 5/5」，混进去会让
  这个数字失去意义。

---

## 5. `error-hints.test.mjs` 在守什么

体检清单 P2-6 的原始问题是「错误文案映射表重复三套，改一处要四处同步」。
收敛后用户可见的失败文案只剩一份：`frontend/electron/error-hints.json`。

- 主进程 `electron/main.cjs` 读 `startup` 段（`require('./error-hints.cjs')` 的 `matchStartup`）；
- 更新器 `electron/updater.cjs` 读 `update` 段（`matchUpdate`）；
- 渲染进程 `src/shared/userFacingErrors.ts` 读 `probe` 段，供 `BackendGate` 兜底。

这个用例文件同时守两件事：**匹配结果**与「**源码里不再出现第二份文案**」。
后一条是静态扫描：把表里的每一句文案拿去 `src/**` 与 `electron/**` 里找
「被写成字符串字面量」的副本，找到就失败——所以谁想把文案抄回组件里，CI 会拦住。

`frontend/scripts/build.mjs` 的构建失败文案不在本次收敛范围内：那个脚本由并行的
仓库治理任务负责归档，改它会产生跨任务冲突；它的文案表留在原处，等该任务落地后再并入
`error-hints.json` 的 `build` 段。

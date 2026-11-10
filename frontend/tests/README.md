# 前端自动化测试

体检清单 `docs/HEALTH_CHECK.md` §4.2 **P1-3「给前端补真实自动化测试」** 的产出。

本目录是**唯一纳入 CI 的前端测试**。
`frontend/scripts/verify-*` 与 `scripts/verify/` 里的脚本是一次性人工验收辅助，
不是自动化测试，也不参与合并门禁（见 `frontend/scripts/README.md`）。

---

## 1. 怎么跑

```bash
cd frontend
pnpm test
```

`pnpm test` 即 `node --test tests/e2e.test.mjs`。
CI（`.github/workflows/ci.yml`）在 `windows-latest` 上跑同一条命令，失败即阻断合并。

不需要先启动后端，也不需要手工起 Vite：测试进程内用 `vite.createServer`
在固定端口 `15173`（`strictPort: true`）启动 dev server，测试结束后自动关闭；
可用环境变量 `E2E_PORT` 改端口。需要本机装有 Playwright 的 Chromium：

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

主链路用例覆盖体检清单建议的「登录 → 建库 → 上传 → 问答」中的
**登录 → 建库 → 问答** 部分。上传依赖 multipart 文件选择与入库任务，
当前离线 mock 不模拟入库流水线，暂未纳入自动化用例；
这一段的验收仍由 `frontend/scripts/verify-*` 人工辅助完成。

---

## 4. 加新用例的约定

- 新接口一律加进 `createMock()` 的 `handle()`，未知接口会返回 `200 {}` 并被记进
  `state.unknown`，写断言前先看它是否被意外命中。
- 每个用例自己 `openWithMock()`，用完 `context.close()`。
- 断言 UI 用 `waitFor()` 轮询，不要拿一次快照判生死。
- 不要在测试里引入真实时间、随机数或外部服务，保持「离线可重复」这条底线。

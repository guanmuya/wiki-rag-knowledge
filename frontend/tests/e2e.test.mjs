/**
 * 前端端到端自动化测试（体检清单 P1-3）。
 *
 * 为什么离线、无凭据也能重复跑：
 *  - 所有 /api/** 请求由 page.route 在浏览器网络层拦截，用确定性的内存 mock 响应；
 *    不启动真实 sidecar、不需要在线模型、不需要固定语料、没有随机种子；
 *  - 回答文本、时间戳、ID、token 全部写死，重复运行结果一致；
 *  - Vite dev server 由测试进程内 createServer 启停（固定端口 + strictPort），
 *    `pnpm test` 一条命令即可，不需要手工先起服务。
 *
 * 覆盖的是真实浏览器 + 真实 React 前端 + 真实 Vite 编译，只把后端换成 mock，
 * 因此是端到端用例，而不是组件快照或纯函数断言。
 *
 * 与 frontend/scripts/verify-* 的区别见 frontend/tests/README.md：
 * 那些是一次性人工验收辅助脚本，本文件才是纳入 CI 的自动化测试。
 */
import { after, before, describe, test } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'
import { chromium } from 'playwright'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const PORT = Number(process.env.E2E_PORT || 15173)
const BASE = `http://127.0.0.1:${PORT}`
const NOW = '2026-01-01T00:00:00Z'
const TOKEN = 'e2e-synthetic-token'
const PASSWORD = 'e2e-Abcdef1234'
const ANSWER = '每晚住宿标准 600 元。'
const QUESTION = '住宿标准是多少？'
const KB_NAME = '自动化测试知识库'
/** 个人 Agent：回答正文与思考过程必须是两条通道（用户 2026-11-11 的截图反馈）。 */
const AGENT_ANSWER = '2026 年中国企业级 AI Agent 市场约 240 亿元。'
const AGENT_THINKING = '用户问的是 2026 年市场规模，先查资料再回答。'
const AGENT_NO_ANSWER = '这次执行只产出了思考过程，没有给出最终回答。可以展开上面的「思考过程」看它做到哪一步。'

let viteServer
let browser

/** 轮询等待：断言 UI 是异步渲染的，不能用一次快照判生死。 */
async function waitFor(check, label, timeout = 15000) {
  const deadline = Date.now() + timeout
  let lastError
  while (Date.now() < deadline) {
    try {
      const value = await check()
      if (value) return value
    } catch (err) {
      lastError = err
    }
    await new Promise((resolvePause) => setTimeout(resolvePause, 50))
  }
  throw new Error(`等待超时：${label}${lastError ? `（${lastError.message}）` : ''}`)
}

const clone = (value) => JSON.parse(JSON.stringify(value))

/**
 * 确定性的内存后端。每个用例一份，互不共享状态，因此可重复、可并发。
 * 未知接口返回 200 {} 而不是打真实代理，避免页面轮询把测试拖挂。
 */
function createMock(options = {}) {
  const user = {
    id: 1, username: 'admin', display_name: '系统管理员', role: 'admin',
    is_active: true, must_change_password: false, created_at: NOW,
  }
  const kbs = [{
    id: 1, name: '员工手册', description: '', mode: 'basic', mode_label: '基础',
    my_level: 'manage', document_count: 0, chunk_count: 0, status: 'ready',
    status_label: '就绪', created_at: NOW, updated_at: NOW,
  }]
  const message = (id, role, content, status = 'ok') => ({
    id, role, content, status, kb_ids: [1], created_at: NOW,
    evidence: [], route: { kb_routes: [] }, latency_ms: 0,
    usage: { calls: 0, latency_ms: 0 }, insufficient: false,
  })
  const conversations = new Map([[1, {
    id: 1, title: '会话 A', kb_ids: [1], pinned: false,
    messages: [message(101, 'user', '会话 A 原有消息')],
    message_count: 1, created_at: NOW, updated_at: NOW,
  }]])
  const listing = () => [...conversations.values()].map(({ messages, ...conv }) => ({
    ...conv, message_count: messages.length,
  }))
  // 个人 Agent 的确定性响应：一条 assistant 消息 = 回答正文（content）+ 思考过程
  // （meta.thinking）。旧实现把推理也塞进 content，气泡里就看不到答案了。
  const agentCore = {
    id: 'codex', name: 'Codex CLI', description: '测试用 CLI 核心', kind: 'cli',
    source: 'builtin', homepage: '', protocols: ['openai'],
    protocol_labels: ['OpenAI 兼容'], bins: ['codex'], installed: true,
    path: 'C:/bin/codex.exe', version: 'codex-cli 0.167.0', enabled: true,
    available: true, unavailable_reason: '', builtin: true, requires_workspace: true,
    timeout_seconds: 900, output: 'jsonl', answer_item_types: ['agent_message'],
    manifest_path: '', compatible_model_count: 1, default_model_id: 7, model_ready: true,
  }
  const agentSession = {
    id: 1, title: '市场规模', core_id: 'codex', core_name: 'Codex CLI',
    model_config_id: 7, model_label: 'Hy4 · Magpie', workspace: 'C:/agent_workspace',
    status: 'idle', message_count: 2, created_at: NOW, updated_at: NOW,
    core_available: true, core_legacy: false,
  }
  const agentMessages = [
    {
      id: 11, session_id: 1, role: 'user', content: '市场规模有多大？',
      core_id: 'codex', core_name: 'Codex CLI', model_label: '', evidence: [],
      meta: {}, status: 'ok', latency_ms: 0, created_at: NOW,
    },
    {
      id: 12, session_id: 1, role: 'assistant', content: AGENT_ANSWER,
      core_id: 'codex', core_name: 'Codex CLI', model_label: 'Hy4', evidence: [],
      meta: { thinking: AGENT_THINKING, usage: null }, status: 'ok',
      latency_ms: 42, created_at: NOW,
    },
  ]
  // 一次执行的完整事件流：推理（thinking）先到，回答（text）后到。
  // 两者分属不同事件类型，正是「回答位置铺满推理」这个 bug 的修复点。
  const agentEvents = [
    { seq: 1, type: 'status', data: { text: '已启动，等待输出…' } },
    { seq: 2, type: 'thinking', data: { text: AGENT_THINKING } },
    { seq: 3, type: 'text', data: { text: AGENT_ANSWER } },
    { seq: 4, type: 'done', data: { status: 'ok' } },
  ]
  const state = {
    loginAttempts: [], createKbRequests: [], askRequests: [], unknown: [],
    // 执行中用例：由用例自己把 finishAgentRun 翻成 true 收尾，避免依赖 sleep。
    finishAgentRun: false,
    agentRunPolls: 0,
    // 错误兜底用例：持续下发结构损坏的响应，直到用例自己把它关掉。
    // 为什么不是「只坏一次」：StrictMode 下页面会双挂载、发两次请求，
    // 「坏一次」的预算可能被一次不会被渲染的请求消费掉，用例会时灵时不灵。
    corruptKb: Boolean(options.corruptKbAlways),
    corruptKbServed: 0,
  }
  let nextId = 1000
  let nextConversationId = 2

  const json = (route, body, status = 200) =>
    route.fulfill({ status, json: body })

  async function handle(route) {
    const req = route.request()
    const path = new URL(req.url()).pathname
    const method = req.method()
    const authorization = req.headers().authorization ?? ''

    if (path === '/api/meta') {
      return await json(route, {
        version: '1.0.17-e2e', app_name: '企业知识库',
        needs_setup: false, offline_models: true,
      })
    }
    if (path === '/api/setup/status') {
      return await json(route, {
        initialized: true, org_name: '测试组织',
        app_name: '企业知识库', version: '1.0.17-e2e',
      })
    }
    if (path === '/api/setup/org') return await json(route, { org_name: '测试组织' })

    if (path === '/api/auth/me') {
      return await json(route, {
        user, permissions: { is_admin: true, kb_grants: [] },
      })
    }
    if (path === '/api/auth/login' && method === 'POST') {
      const body = req.postDataJSON()
      state.loginAttempts.push(clone(body))
      if (options.loginFails || body.password !== PASSWORD) {
        return await json(route, { detail: '用户名或密码错误' }, 401)
      }
      return await json(route, { token: TOKEN, user })
    }
    if (path === '/api/auth/logout' && method === 'POST') {
      return await json(route, { ok: true })
    }

    if (path === '/api/kbs' && method === 'GET') {
      // corruptKbAlways：把 name 换成一个对象。React 渲染 `{kb.name}` 会抛
      // 「Objects are not valid as a React child」，正好用来验证 ErrorBoundary 兜底。
      // 用例断言完兜底页后把 state.corruptKb 置为 false，再验证能恢复正常列表。
      if (state.corruptKb) {
        state.corruptKbServed += 1
        return await json(route, {
          items: [{ ...kbs[0], name: { broken: true } }],
          total: kbs.length,
        })
      }
      return await json(route, { items: kbs, total: kbs.length })
    }
    if (path === '/api/kbs' && method === 'POST') {
      const body = req.postDataJSON()
      state.createKbRequests.push(clone(body))
      const created = {
        ...kbs[0], id: ++nextId, name: body.name,
        description: body.description || '', mode: body.mode || 'basic',
        created_at: NOW, updated_at: NOW,
      }
      kbs.push(created)
      return await json(route, created, 201)
    }
    const kbMatch = path.match(/^\/api\/kbs\/(\d+)$/)
    if (kbMatch) {
      const kb = kbs.find((item) => item.id === Number(kbMatch[1]))
      return await json(route, kb ?? {}, kb ? 200 : 404)
    }

    if (path === '/api/chat/models') {
      return await json(route, { items: [], default_id: null })
    }

    // ---- 个人 Agent（思考过程与回答分区）
    if (path === '/api/agent/cores') {
      return await json(route, {
        items: [agentCore], errors: [], plugin_dir: 'data/agent_plugins', schema: 1,
        supported_protocols: [{ value: 'openai', label: 'OpenAI 兼容' }],
      })
    }
    if (path === '/api/agent/models') {
      return await json(route, {
        core_id: 'codex',
        items: [{
          id: 7, model: 'hy4', label: 'Hy4', provider_id: 1, provider: 'Magpie',
          protocol: 'openai', protocol_label: 'OpenAI 兼容', is_default: true,
          compatible: true, display: 'Hy4 · Magpie',
        }],
      })
    }
    if (path === '/api/agent/sessions' && method === 'GET') {
      return await json(route, { items: [agentSession], total: 1 })
    }
    if (path === '/api/agent/sessions/1/messages' && method === 'POST') {
      const body = req.postDataJSON()
      return await json(route, {
        run_id: 22,
        user_message: { ...agentMessages[0], id: 21, content: body.content },
        assistant_message: {
          ...agentMessages[1], id: 22, content: '', status: 'pending', meta: {},
        },
        session: { ...agentSession, status: 'running' },
      })
    }
    if (path === '/api/agent/sessions/1') {
      const live = options.agentScenario === 'live'
      const noAnswer = options.agentScenario === 'no-answer'
      const finished = clone(agentMessages)
      if (noAnswer) {
        finished[1] = {
          ...finished[1], status: 'failed', content: AGENT_NO_ANSWER,
          meta: { thinking: AGENT_THINKING, error: AGENT_NO_ANSWER, usage: null },
        }
      }
      const messages = live && !state.finishAgentRun ? [] : finished
      return await json(route, {
        ...agentSession, messages, message_count: messages.length,
        core: agentCore, running: false,
      })
    }
    const agentRunMatch = path.match(/^\/api\/agent\/runs\/(\d+)\/events$/)
    if (agentRunMatch) {
      const after = Number(new URL(req.url()).searchParams.get('after') ?? 0)
      state.agentRunPolls += 1
      // 执行中只下发状态与推理；由用例翻旗后才下发回答并收尾。
      const visible = state.finishAgentRun
        ? agentEvents
        : agentEvents.filter((event) => event.seq <= 2)
      const events = visible.filter((event) => event.seq > after)
      return await json(route, {
        events: clone(events),
        next: events.length ? events[events.length - 1].seq : after,
        status: state.finishAgentRun ? 'ok' : 'running',
        error: '',
        ...(state.finishAgentRun ? { message: clone(agentMessages[1]) } : {}),
        run_id: Number(agentRunMatch[1]), session_id: 1,
      })
    }
    if (path === '/api/conversations' && method === 'GET') {
      return await json(route, { items: listing(), total: conversations.size })
    }
    if (path === '/api/conversations' && method === 'POST') {
      const body = req.postDataJSON()
      const id = nextConversationId++
      const created = {
        id, title: body.title, kb_ids: body.kb_ids ?? [], messages: [],
        message_count: 0, pinned: false, created_at: NOW, updated_at: NOW,
      }
      conversations.set(id, created)
      return await json(route, created, 201)
    }

    // 流式问答：SSE 的 done 事件就是完整 AskResponse。写死文本，无随机性。
    const streamMatch = path.match(/^\/api\/conversations\/(\d+)\/ask\/stream$/)
    if (streamMatch && method === 'POST') {
      const id = Number(streamMatch[1])
      const body = req.postDataJSON()
      state.askRequests.push({ id, body: clone(body), authorization })
      const question = message(++nextId, 'user', body.question)
      const answer = message(++nextId, 'assistant', ANSWER)
      const conv = conversations.get(id)
      if (conv) {
        conv.messages.push(question, answer)
        conv.message_count = conv.messages.length
        conv.updated_at = NOW
      }
      const done = {
        type: 'done', message: answer, evidence: [],
        route: { kb_routes: [] }, usage: { calls: 0, latency_ms: 42 },
        insufficient: false, suggestions: [], refusal_reason: '', denied: [],
      }
      const sse = [
        `data: ${JSON.stringify({ type: 'answer', delta: ANSWER })}`, '',
        `data: ${JSON.stringify(done)}`, '', '',
      ].join('\n')
      return await route.fulfill({
        status: 200, contentType: 'text/event-stream; charset=utf-8', body: sse,
      })
    }

    const detailMatch = path.match(/^\/api\/conversations\/(\d+)$/)
    if (detailMatch) {
      const id = Number(detailMatch[1])
      const conv = conversations.get(id)
      if (!conv) return await json(route, { detail: '会话不存在' }, 404)
      if (method === 'PATCH') {
        Object.assign(conv, req.postDataJSON())
        return await json(route, listing().find((item) => item.id === id))
      }
      if (method === 'DELETE') {
        conversations.delete(id)
        return await json(route, { ok: true })
      }
      return await json(route, clone(conv))
    }

    state.unknown.push(`${method} ${path}`)
    return await json(route, {})
  }

  return { handle, state }
}

/** 每个用例一个独立浏览器上下文：localStorage / 路由状态互不污染。 */
async function openWithMock(options = {}) {
  const mock = createMock(options)
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 }, locale: 'zh-CN',
  })
  if (options.loggedIn) {
    await context.addInitScript((token) => {
      localStorage.setItem('wrk_token', token)
    }, TOKEN)
  }
  await context.route('**/api/**', (route) => mock.handle(route))
  const page = await context.newPage()
  const pageErrors = []
  page.on('pageerror', (err) => pageErrors.push(err.message))
  return { context, page, mock, pageErrors }
}

describe('前端主链路 E2E（离线、可重复、纳入 CI）', () => {
  before(async () => {
    viteServer = await createServer({
      root: ROOT,
      configFile: resolve(ROOT, 'vite.config.ts'),
      base: '/',
      logLevel: 'error',
      server: { host: '127.0.0.1', port: PORT, strictPort: true },
    })
    await viteServer.listen()
    browser = await chromium.launch({ headless: true })
  })

  after(async () => {
    await browser?.close()
    await viteServer?.close()
  })

  test('未登录访问业务页被送回登录页', { timeout: 60000 }, async () => {
    const { context, page, mock } = await openWithMock()
    try {
      await page.goto(`${BASE}/#/kbs`, { waitUntil: 'domcontentloaded' })
      await page.getByRole('heading', { name: '登录', exact: true }).waitFor({ timeout: 20000 })
      assert.equal(new URL(page.url()).hash, '#/login')
      assert.deepEqual(mock.state.loginAttempts, [])
      assert.deepEqual(mock.state.askRequests, [])
    } finally {
      await context.close()
    }
  })

  test('带有效令牌访问业务页：恢复登录态，不被送回登录页', { timeout: 60000 }, async () => {
    const { context, page, mock } = await openWithMock({ loggedIn: true })
    try {
      await page.goto(`${BASE}/#/kbs`, { waitUntil: 'domcontentloaded' })
      await waitFor(
        () => page.getByRole('button', { name: '+ 新建知识库' }).isVisible().catch(() => false),
        '知识库列表渲染（管理员可见新建按钮）',
        20000,
      )
      assert.equal(new URL(page.url()).hash, '#/kbs')
      assert.equal(await page.getByRole('heading', { name: '登录', exact: true }).count(), 0)
      assert.deepEqual(mock.state.loginAttempts, [])
    } finally {
      await context.close()
    }
  })

  test('登录凭据错误：停留在登录页并显示服务端错误', { timeout: 60000 }, async () => {
    const { context, page, mock } = await openWithMock({ loginFails: true })
    try {
      await page.goto(`${BASE}/#/login`, { waitUntil: 'domcontentloaded' })
      const heading = page.getByRole('heading', { name: '登录', exact: true })
      await heading.waitFor({ timeout: 20000 })
      await page.getByPlaceholder('请输入用户名').fill('admin')
      await page.getByPlaceholder('请输入密码').fill('wrong-password')
      await page.locator('.login-card').getByRole('button', { name: '登录', exact: true }).click()

      const error = await waitFor(
        () => page.locator('.login-error').innerText().catch(() => ''),
        '登录错误提示出现',
      )
      assert.match(error, /用户名或密码错误|错误|失败/)
      assert.equal(new URL(page.url()).hash, '#/login')
      assert.equal(mock.state.loginAttempts.length, 1)
      assert.equal(mock.state.loginAttempts[0].username, 'admin')
    } finally {
      await context.close()
    }
  })

  test('登录 → 新建知识库 → 智能问答收到回答', { timeout: 90000 }, async () => {
    const { context, page, mock, pageErrors } = await openWithMock()
    try {
      await page.goto(`${BASE}/#/login`, { waitUntil: 'domcontentloaded' })
      await page.getByRole('heading', { name: '登录', exact: true }).waitFor({ timeout: 20000 })
      await page.getByPlaceholder('请输入用户名').fill('admin')
      await page.getByPlaceholder('请输入密码').fill(PASSWORD)
      await page.locator('.login-card').getByRole('button', { name: '登录', exact: true }).click()

      // 1) 登录成功后进入知识库列表
      await waitFor(
        async () => new URL(page.url()).hash === '#/kbs',
        '登录后跳转到知识库列表',
      )
      const createButton = page.getByRole('button', { name: '+ 新建知识库' })
      await createButton.waitFor({ timeout: 20000 })
      assert.equal(mock.state.loginAttempts.length, 1)

      // 2) 新建知识库，断言请求真的发出、列表真的刷新
      await createButton.click()
      await page.getByPlaceholder('例如：员工手册').fill(KB_NAME)
      await page.getByRole('button', { name: '创建', exact: true }).click()
      await waitFor(
        () => page.getByText(KB_NAME, { exact: true }).count().then((n) => n > 0),
        '新建的知识库出现在列表',
      )
      assert.equal(mock.state.createKbRequests.length, 1)
      assert.equal(mock.state.createKbRequests[0].name, KB_NAME)

      // 3) 进入智能问答，提问并收到确定性的回答
      await page.goto(`${BASE}/#/chat/1`, { waitUntil: 'domcontentloaded' })
      const composer = page.getByRole('textbox', { name: '提问输入框' })
      await waitFor(() => composer.isEnabled(), '问答输入框可用', 20000)
      await composer.fill(QUESTION)
      await composer.press('Enter')

      await waitFor(
        () => mock.state.askRequests.length > 0,
        '流式问答请求已发出',
        20000,
      )
      const ask = mock.state.askRequests[0]
      assert.equal(ask.body.question, QUESTION)
      assert.equal(ask.authorization, `Bearer ${TOKEN}`)
      await waitFor(
        () => page.locator('.chat-thread').getByText(ANSWER, { exact: true }).count().then((n) => n > 0),
        '回答渲染在会话区',
        20000,
      )
      assert.deepEqual(pageErrors, [], `页面不应有未捕获异常：${pageErrors.join('; ')}`)
    } finally {
      await context.close()
    }
  })

  /**
   * 用户 2026-11-11 的截图反馈：「针对这种思考是不是需要单独做一个隔离呢？要不然
   * 用户看你这个得不到最终想要的答案吖」——气泡里铺满模型推理，答案反而没出现。
   *
   * 判据（真实 React 渲染，不 mock 组件）：
   * 1. 回答正文出现在回答气泡里；
   * 2. 推理**不**在回答气泡里，而是收在默认折叠的「思考过程」；
   * 3. 点开思考过程能看到推理原文。
   */
  test('个人 Agent：思考过程与最终回答分开显示', { timeout: 60000 }, async () => {
    const { context, page, pageErrors } = await openWithMock({ loggedIn: true })
    try {
      await page.goto(`${BASE}/#/agent`, { waitUntil: 'domcontentloaded' })
      const bubble = page.locator('.msg-assistant .msg-bubble').first()
      await waitFor(
        async () => (await bubble.innerText()).includes(AGENT_ANSWER),
        '回答正文渲染在主气泡里',
        20000,
      )
      const bubbleText = await bubble.innerText()
      assert.ok(
        !bubbleText.includes('用户问的是'),
        `推理不该出现在回答气泡里：${bubbleText.slice(0, 200)}`,
      )

      const card = page.locator('.thinking-card').first()
      await card.waitFor({ timeout: 20000 })
      const head = card.locator('.thinking-head')
      assert.match(await head.innerText(), /思考过程/)
      assert.equal(await head.getAttribute('aria-expanded'), 'false')
      assert.equal(
        await card.locator('.agent-thinking-body').count(), 0,
        '思考过程默认应当是收起的',
      )

      await head.click()
      await waitFor(
        async () => (await card.innerText()).includes('用户问的是'),
        '展开思考过程后能看到推理原文',
      )
      assert.deepEqual(pageErrors, [], `页面不应有未捕获异常：${pageErrors.join('; ')}`)
    } finally {
      await context.close()
    }
  })

  /** 一轮只出推理、没有最终回答：说明原因，但过程仍可展开核对。 */
  test('个人 Agent：只出推理时说明没有答案，过程仍可展开', { timeout: 60000 }, async () => {
    const { context, page, pageErrors } = await openWithMock({
      loggedIn: true, agentScenario: 'no-answer',
    })
    try {
      await page.goto(`${BASE}/#/agent`, { waitUntil: 'domcontentloaded' })
      const bubble = page.locator('.msg-assistant .msg-bubble').first()
      await waitFor(
        async () => (await bubble.innerText()).includes('没有给出最终回答'),
        '气泡里说明「没有给出最终回答」',
        20000,
      )
      const bubbleText = await bubble.innerText()
      assert.ok(
        !bubbleText.includes('用户问的是'),
        `推理不该被当成回答倒进气泡：${bubbleText.slice(0, 200)}`,
      )
      assert.ok(!bubbleText.includes(AGENT_ANSWER), '没有答案时不该编出答案')

      const card = page.locator('.thinking-card').first()
      await card.waitFor({ timeout: 20000 })
      const head = card.locator('.thinking-head')
      assert.match(await head.innerText(), /思考过程/)
      assert.equal(await head.getAttribute('aria-expanded'), 'false')
      await head.click()
      await waitFor(
        async () => (await card.innerText()).includes('用户问的是'),
        '展开后能看到那轮推理，便于核对卡在哪一步',
      )
      assert.deepEqual(pageErrors, [], `页面不应有未捕获异常：${pageErrors.join('; ')}`)
    } finally {
      await context.close()
    }
  })

  /** 用户截图发生的正是执行中：推理在流，答案还没出来。 */
  test('个人 Agent：执行中推理只进折叠区，答案随后落进气泡', { timeout: 60000 }, async () => {
    const { context, page, mock, pageErrors } = await openWithMock({
      loggedIn: true, agentScenario: 'live',
    })
    try {
      await page.goto(`${BASE}/#/agent`, { waitUntil: 'domcontentloaded' })
      const composer = page.locator('.chat-composer textarea')
      await waitFor(() => composer.isEnabled(), '个人 Agent 输入框可用', 20000)
      await composer.fill('市场规模有多大？')
      await composer.press('Enter')

      const card = page.locator('.thinking-card').first()
      await card.waitFor({ timeout: 20000 })
      const head = card.locator('.thinking-head')
      assert.match(await head.innerText(), /正在思考/)
      assert.equal(await head.getAttribute('aria-expanded'), 'false')

      const liveBubble = page.locator('.msg-bubble.agent-live').first()
      const liveText = await liveBubble.innerText()
      assert.ok(liveText.includes('正在思考'), `答案区应提示正在思考：${liveText.slice(0, 200)}`)
      assert.ok(
        !liveText.includes('用户问的是'),
        `推理不该铺在回答位置：${liveText.slice(0, 200)}`,
      )
      assert.ok(!liveText.includes(AGENT_ANSWER), '答案还没到，不该先出现')

      // 由用例收尾，不依赖 sleep：下一轮轮询下发回答与 done。
      mock.state.finishAgentRun = true
      const answerBubble = page.locator('.msg-assistant .msg-bubble').last()
      await waitFor(
        async () => (await answerBubble.innerText()).includes(AGENT_ANSWER),
        '回答落到气泡里',
        20000,
      )
      assert.ok(!(await answerBubble.innerText()).includes('用户问的是'))
      assert.equal(
        await page.locator('.thinking-card .thinking-head').last().getAttribute('aria-expanded'),
        'false',
        '落库后的思考过程仍应默认折叠',
      )
      assert.ok(mock.state.agentRunPolls >= 2, '事件流确实被轮询过')
      assert.deepEqual(pageErrors, [], `页面不应有未捕获异常：${pageErrors.join('; ')}`)
    } finally {
      await context.close()
    }
  })

  /**
   * 体检清单 P1-10 的验收：「制造一次页面渲染异常时显示兜底页而非白屏」。
   * 这里不 mock 组件，而是让后端返回结构损坏的 KB（name 是对象），
   * 由真实 React 渲染真的抛错——只有 ErrorBoundary 真接住了才会出现兜底页。
   */
  test('页面渲染异常时显示兜底页而不是白屏，且能从兜底页恢复', { timeout: 60000 }, async () => {
    const { context, page, mock } = await openWithMock({
      loggedIn: true,
      corruptKbAlways: true,
    })
    try {
      await page.goto(`${BASE}/#/kbs`, { waitUntil: 'domcontentloaded' })

      // 1) 兜底页出现，而不是一片空白
      const heading = page.getByRole('heading', { name: '界面出了点问题' })
      await heading.waitFor({ timeout: 20000 })
      assert.equal(await heading.count(), 1)
      assert.ok(mock.state.corruptKbServed >= 1, '损坏的响应确实被下发过')

      // 2) 兜底页可读、有自救入口（白屏的话这里是空串）
      const text = await page.locator('#root').innerText()
      assert.ok(text.includes('重试'), `兜底页缺「重试」入口：${text.slice(0, 200)}`)
      assert.ok(text.includes('回到首页'), `兜底页缺「回到首页」入口：${text.slice(0, 200)}`)

      // 3) 数据恢复正常后，从兜底页能回到业务页（boundary 复位成功）
      mock.state.corruptKb = false
      await page.getByRole('button', { name: '回到首页' }).click()
      await waitFor(
        () => page.getByRole('button', { name: '+ 新建知识库' }).isVisible().catch(() => false),
        '从兜底页恢复到知识库列表',
        20000,
      )
      assert.equal(await page.locator('.backend-down').count(), 0)
    } finally {
      await context.close()
    }
  })
})

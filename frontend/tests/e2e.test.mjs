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
  const state = { loginAttempts: [], createKbRequests: [], askRequests: [], unknown: [] }
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
})

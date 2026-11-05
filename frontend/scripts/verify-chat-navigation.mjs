/**
 * Browser regressions for chat requests that outlive the current conversation.
 *
 * Start Vite separately on 127.0.0.1:15173, then run from frontend:
 *   node scripts/verify-chat-navigation.mjs
 * Set CHAT_TEST_BASE_URL to test a different running Vite server.
 * Use --case=<substring> to run matching cases only.
 * Add --screenshot=before or --screenshot=after to save the matching in-flight
 * A / selected B view, or --screenshot=recovery for the two-failure queue, under
 * .tmp-verify/chat-race/. All /api requests are
 * intercepted; this script never sends test messages to a real backend. Each
 * case has its own browser context and in-memory server. A nonzero exit means
 * a regression.
 */
import { chromium } from 'playwright'
import { strict as assert } from 'node:assert'
import { mkdir } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const BASE = (process.env.CHAT_TEST_BASE_URL || 'http://127.0.0.1:15173').replace(/\/+$/, '')
const caseFilter = process.argv.find((arg) => arg.startsWith('--case='))?.slice('--case='.length).toLowerCase()
const screenshotArg = process.argv.find((arg) => arg === '--screenshot' || arg.startsWith('--screenshot='))
const screenshotPhase = screenshotArg === '--screenshot' ? 'before' : screenshotArg?.split('=')[1]
if (screenshotPhase && !['before', 'after', 'recovery'].includes(screenshotPhase)) {
  throw new Error('Use --screenshot=before, --screenshot=after or --screenshot=recovery')
}
const OUT = screenshotPhase
  ? resolve(dirname(fileURLToPath(import.meta.url)), `../.tmp-verify/chat-race/${screenshotPhase}.png`)
  : null
const QUESTION = 'A 的新问题：住宿标准是多少？'
const ANSWER = 'A 的回答：每晚 600 元。'
const B_QUESTION = 'B 的新问题：审批需要多久？'
const B_ANSWER = 'B 的回答：审批需要五天。'
const A_HISTORY = '会话 A 原有消息'
const B_HISTORY = '会话 B 原有消息'
const GENERATING = '正在检索并生成回答…'
const now = '2026-01-01T00:00:00Z'
const clone = (value) => JSON.parse(JSON.stringify(value))
const pause = (ms) => new Promise((resolvePause) => setTimeout(resolvePause, ms))

function deferred() {
  let resolvePromise
  const promise = new Promise((resolveValue) => { resolvePromise = resolveValue })
  return { promise, resolve: resolvePromise }
}

async function waitFor(check, label, timeout = 5000) {
  const deadline = Date.now() + timeout
  let lastError
  while (Date.now() < deadline) {
    try {
      const value = await check()
      if (value) return value
    } catch (err) {
      lastError = err
    }
    await pause(50)
  }
  throw new Error(`Timed out: ${label}${lastError ? ` (${lastError.message})` : ''}`)
}

async function waitPromise(promise, label, timeout = 5000) {
  let timer
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`Timed out: ${label}`)), timeout) }),
    ])
  } finally {
    clearTimeout(timer)
  }
}

function message(id, role, content, status = 'ok') {
  return {
    id, role, content, status, kb_ids: [1], created_at: now,
    evidence: [], route: { kb_routes: [] }, latency_ms: 0,
    usage: { calls: 0, latency_ms: 0 }, insufficient: false,
  }
}

function conversation(id, title, messages) {
  return {
    id, title, kb_ids: [1], messages, message_count: messages.length,
    created_at: now, updated_at: now,
  }
}

function mockServer({ differentKbs = false } = {}) {
  const data = new Map([
    [1, conversation(1, '会话 A', [message(101, 'user', A_HISTORY)])],
    [2, conversation(2, '会话 B', [message(201, 'user', B_HISTORY)])],
  ])
  if (differentKbs) data.get(2).kb_ids = [2]
  const knowledgeBases = [
    { id: 1, name: '测试知识库', mode: 'basic', mode_label: '基础', my_level: 'manage' },
    ...(differentKbs ? [{ id: 2, name: '会话 B 知识库', mode: 'basic', mode_label: '基础', my_level: 'manage' }] : []),
  ]
  const asks = new Map()
  const askRequests = []
  const patchRequests = []
  const createRequests = []
  const holds = new Map()
  const failedGets = new Map()
  const rejectedAsks = new Map()
  const held = []
  const unknown = []
  const createHolds = []
  const users = {
    tester: { id: 1, username: 'tester', display_name: '测试用户', role: 'admin' },
    other: { id: 2, username: 'other', display_name: '另一个用户', role: 'admin' },
  }
  const sessions = new Map([['synthetic-test-token', users.tester]])
  let nextSessionId = 0
  let nextMessageId = 1000
  let nextConversationId = 3

  function listing() {
    return [...data.values()].map(({ messages, ...conv }) => ({
      ...conv, message_count: messages.length,
    }))
  }

  function finishServer(id) {
    const ask = asks.get(id)
    if (!ask) throw new Error(`No in-flight ask for ${id}`)
    Object.assign(ask.answer, { status: 'ok', content: id === 2 ? B_ANSWER : ANSWER, latency_ms: 42 })
  }

  function successResponse(ask) {
    return {
      message: clone(ask.answer), evidence: [], route: { kb_routes: [] },
      usage: { calls: 0, latency_ms: 42 }, insufficient: false,
      suggestions: [], refusal_reason: '', denied: [],
    }
  }

  async function reply(route, json, status = 200) {
    await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(json) })
  }

  async function handle(route) {
    const req = route.request()
    const path = new URL(req.url()).pathname
    const method = req.method()
    try {
      if (path === '/api/meta') return await reply(route, { version: 'test', needs_setup: false, offline_models: false })
      if (path === '/api/auth/me') {
        const token = (req.headers().authorization ?? '').replace(/^Bearer /, '')
        return await reply(route, {
          user: sessions.get(token) ?? users.tester,
          permissions: { is_admin: true, kb_grants: [] },
        })
      }
      if (path === '/api/auth/login' && method === 'POST') {
        const user = users[req.postDataJSON().username]
        if (!user) return await reply(route, { detail: '模拟用户不存在' }, 401)
        const token = `synthetic-session-${++nextSessionId}-${user.username}`
        sessions.set(token, user)
        return await reply(route, { token, user })
      }
      if (path === '/api/auth/logout' && method === 'POST') return await reply(route, { ok: true })
      if (path === '/api/setup/org') return await reply(route, { org_name: '测试组织' })
      if (path === '/api/kbs') return await reply(route, {
        items: knowledgeBases, total: knowledgeBases.length,
      })
      if (path === '/api/conversations' && method === 'GET') return await reply(route, { items: listing(), total: data.size })
      if (path === '/api/conversations' && method === 'POST') {
        const body = req.postDataJSON()
        const id = nextConversationId++
        const createHold = createHolds.shift()
        createRequests.push({ id, body: clone(body), authorization: req.headers().authorization ?? '' })
        const created = conversation(id, body.title, [])
        created.kb_ids = Array.isArray(body.kb_ids) ? [...body.kb_ids] : []
        let createOutcome
        if (createHold) {
          createHold.arrived.resolve()
          createOutcome = await createHold.release.promise
        }
        try {
          if (createOutcome === 'fail') return await reply(route, { detail: '模拟创建会话失败' }, 500)
          data.set(id, created)
          return await reply(route, listing().find((c) => c.id === id), 201)
        } finally {
          if (createHold) createHold.responded.resolve()
        }
      }
      const match = path.match(/^\/api\/conversations\/(\d+)(\/ask)?$/)
      if (match) {
        const id = Number(match[1])
        const conv = data.get(id)
        if (!conv) return await reply(route, { detail: '会话不存在' }, 404)
        if (method === 'GET' && !match[2]) {
          const failed = failedGets.get(id)?.shift()
          if (failed) {
            try {
              return await reply(route, { detail: '模拟会话读取失败' }, 500)
            } finally {
              failed.responded.resolve()
            }
          }
          // Snapshot on arrival, not release: delayed responses model stale reads.
          const snapshot = clone({ ...conv, message_count: conv.messages.length })
          const hold = holds.get(id)?.shift()
          if (hold) {
            hold.arrived.resolve()
            await hold.release.promise
          }
          try {
            await reply(route, snapshot)
          } finally {
            if (hold) hold.responded.resolve()
          }
          return
        }
        if (method === 'POST' && match[2]) {
          const body = req.postDataJSON()
          askRequests.push({ id, body: clone(body), authorization: req.headers().authorization ?? '' })
          const rejection = rejectedAsks.get(id)?.shift()
          if (rejection) {
            rejection.arrived.resolve()
            await rejection.release.promise
            try {
              return await reply(route, { detail: rejection.status === 401 ? '模拟登录已失效' : '模拟知识库权限不足' }, rejection.status)
            } finally {
              rejection.responded.resolve()
            }
          }
          const question = message(++nextMessageId, 'user', body.question)
          const answer = message(++nextMessageId, 'assistant', '', 'pending')
          conv.messages.push(question, answer)
          conv.message_count = conv.messages.length
          const ask = { answer, released: deferred(), responded: deferred() }
          asks.set(id, ask)
          const outcome = await ask.released.promise
          try {
            if (outcome === 'drop') await route.abort('failed')
            else await reply(route, successResponse(ask))
          } finally {
            // The UI may cancel its client-side fetch after navigation. The
            // simulated server still completes and stores the answer.
            ask.responded.resolve()
          }
          return
        }
        if (method === 'PATCH') {
          const body = req.postDataJSON()
          patchRequests.push({ id, body: clone(body) })
          Object.assign(conv, body)
          return await reply(route, { ...conv, messages: undefined })
        }
      }
      unknown.push(`${method} ${path}`)
      await reply(route, { detail: `Unexpected mocked API request: ${method} ${path}` }, 501)
    } catch (err) {
      // Context closure or intentional fetch cancellation can invalidate a
      // deliberately held route. Preserve other errors as test failures.
      if (!req.failure() && !/closed|Target page|route.*handled|Invalid InterceptionId/i.test(String(err))) throw err
    }
  }

  return {
    handle, unknown, askRequests, patchRequests, createRequests,
    async waitForAsk(id) { return await waitFor(() => asks.get(id), `ask ${id} to reach server`) },
    async finishAsk(id) {
      const ask = await this.waitForAsk(id)
      finishServer(id)
      ask.released.resolve('ok')
      await waitPromise(ask.responded.promise, `ask ${id} response delivered`)
    },
    async dropAsk(id) {
      const ask = await this.waitForAsk(id)
      ask.released.resolve('drop')
      await waitPromise(ask.responded.promise, `ask ${id} response delivered`)
    },
    finishServer,
    rejectNextAsk(id, status = 403) {
      const rejection = { status, arrived: deferred(), release: deferred(), responded: deferred() }
      if (!rejectedAsks.has(id)) rejectedAsks.set(id, [])
      rejectedAsks.get(id).push(rejection)
      held.push(rejection)
      return rejection
    },
    insertExternalTurn(id, text, kbIds) {
      const conv = data.get(id)
      if (!conv) throw new Error(`No conversation ${id}`)
      const question = { ...message(++nextMessageId, 'user', text), kb_ids: [...kbIds] }
      const answer = { ...message(++nextMessageId, 'assistant', '其他客户端的回答'), kb_ids: [...kbIds] }
      conv.messages.push(question, answer)
      conv.message_count = conv.messages.length
    },
    failNextGet(id) {
      const failure = { responded: deferred() }
      if (!failedGets.has(id)) failedGets.set(id, [])
      failedGets.get(id).push(failure)
      return failure
    },
    holdNextGet(id) {
      const hold = { arrived: deferred(), release: deferred(), responded: deferred() }
      if (!holds.has(id)) holds.set(id, [])
      holds.get(id).push(hold)
      held.push(hold)
      return hold
    },
    holdCreate() {
      const createHold = { arrived: deferred(), release: deferred(), responded: deferred() }
      createHolds.push(createHold)
      held.push(createHold)
      return createHold
    },
    releaseAll() {
      for (const item of held) item.release.resolve()
      for (const ask of asks.values()) ask.released.resolve('drop')
    },
  }
}

const browser = await chromium.launch({ headless: true })
const results = []

async function runCase(name, fn, options = {}) {
  if (caseFilter && !name.toLowerCase().includes(caseFilter)) return
  const server = mockServer(options)
  const context = await browser.newContext({
    viewport: options.viewport ?? { width: 1440, height: 900 }, locale: 'zh-CN',
  })
  const pageErrors = []
  await context.addInitScript(() => localStorage.setItem('wrk_token', 'synthetic-test-token'))
  await context.route('**/api/**', (route) => server.handle(route))
  const page = await context.newPage()
  page.on('pageerror', (err) => pageErrors.push(err.message))
  const failures = []
  async function check(label, condition) {
    try {
      assert.ok(await condition(), label)
    } catch (err) {
      failures.push(`${label}: ${err.message}`)
    }
  }
  try {
    await fn({ page, server, check })
    if (server.unknown.length) failures.push(`Unexpected API calls: ${server.unknown.join(', ')}`)
    if (pageErrors.length) failures.push(`Browser errors: ${pageErrors.join('; ')}`)
  } catch (err) {
    failures.push(err.stack || String(err))
  } finally {
    server.releaseAll()
    await context.close()
  }
  results.push({ name, failures })
  console.log(`${failures.length ? '[FAIL]' : '[PASS]'} ${name}`)
  for (const failure of failures) console.log(`  ${failure.replaceAll('\n', '\n  ')}`)
}

const thread = (page) => page.locator('.chat-thread')
const composer = (page) => page.getByRole('textbox', { name: '提问输入框' })
const countText = (page, text) => thread(page).getByText(text, { exact: true }).count()
const recoveryGroup = (page, text) => page.getByRole('group', { name: `待处理提问：${text}`, exact: true })
const recoveryAction = (group, text) => group.getByRole('button', { name: `恢复未发送的问题：${text}`, exact: true })
const dismissAction = (group, text) => group.getByRole('button', { name: `忽略这条提问：${text}`, exact: true })
const select = async (page, title, id, marker) => {
  await page.locator('.chat-sidebar').getByRole('link', { name: title }).click()
  await page.waitForFunction((value) => location.hash === `#/chat/${value}`, id)
  if (marker) await thread(page).getByText(marker, { exact: true }).first().waitFor({ timeout: 5000 })
}
const open = async (page, id = 1) => {
  await page.goto(`${BASE}/#/chat/${id}`, { waitUntil: 'domcontentloaded' })
  await page.locator('.chat-sidebar').getByRole('link', { name: '会话 A' }).waitFor({ timeout: 10000 })
  await thread(page).getByText(id === 1 ? A_HISTORY : B_HISTORY, { exact: true }).waitFor({ timeout: 10000 })
  await waitFor(() => composer(page).isEnabled(), 'initial composer enabled')
}
const askQuestion = async (page, server) => {
  await composer(page).fill(QUESTION)
  await composer(page).press('Enter')
  await server.waitForAsk(1)
}
const settle = async (page) => { await page.waitForTimeout(180) }
const answerVisible = (page) => waitFor(() => countText(page, ANSWER).then((n) => n > 0), 'answer visible', 6500)

try {
  await runCase('A in flight, switch to B, finish A and return after completion', async ({ page, server, check }) => {
    await open(page)
    await askQuestion(page, server)
    await select(page, '会话 B', 2, B_HISTORY)
    await settle(page)
    if (OUT && screenshotPhase !== 'recovery') {
      await mkdir(dirname(OUT), { recursive: true })
      await page.screenshot({ path: OUT, animations: 'disabled' })
      console.log(`  Screenshot: ${OUT}`)
    }
    await check('B must not show A’s optimistic question', async () => (await countText(page, QUESTION)) === 0)
    await check('B composer must remain usable while A is pending', () => composer(page).isEnabled())
    await server.finishAsk(1)
    await settle(page)
    await check('A completion must keep B selected', async () => (await page.evaluate(() => location.hash)) === '#/chat/2')
    await check('A completion must keep B’s messages visible', async () => (await countText(page, B_HISTORY)) === 1)
    await check('A completion must not render A’s answer in B', async () => (await countText(page, ANSWER)) === 0)
    await select(page, '会话 A', 1, A_HISTORY)
    await answerVisible(page)
    await check('returning to A after completion shows one question', async () => (await countText(page, QUESTION)) === 1)
    await check('returning to A after completion shows one answer', async () => (await countText(page, ANSWER)) === 1)
  })

  await runCase('mobile conversation drawer switches A to B while A is pending', async ({ page, server, check }) => {
    await page.goto(`${BASE}/#/chat/1`, { waitUntil: 'domcontentloaded' })
    await thread(page).getByText(A_HISTORY, { exact: true }).waitFor({ timeout: 10000 })
    await waitFor(() => composer(page).isEnabled(), 'mobile A composer enabled')
    await askQuestion(page, server)
    const toggle = page.getByRole('button', { name: '会话列表', exact: true })
    await toggle.click()
    await select(page, '会话 B', 2, B_HISTORY)
    await check('conversation drawer closes after choosing B', async () => (await toggle.getAttribute('aria-expanded')) === 'false')
    await check('mobile B does not show A’s pending question', async () => (await countText(page, QUESTION)) === 0)
    await check('mobile B composer remains enabled', () => composer(page).isEnabled())
    await server.finishAsk(1)
    await settle(page)
    await check('A completion preserves mobile B content', async () =>
      (await page.evaluate(() => location.hash)) === '#/chat/2' && (await countText(page, B_HISTORY)) === 1)
    await check('mobile B does not show A’s answer', async () => (await countText(page, ANSWER)) === 0)
    await check('mobile B composer stays enabled', () => composer(page).isEnabled())
  }, { viewport: { width: 390, height: 844 } })

  await runCase('A and B can finish independently when both have an ask', async ({ page, server, check }) => {
    await open(page)
    await askQuestion(page, server)
    await select(page, '会话 B', 2, B_HISTORY)
    const canAskInB = await composer(page).isEnabled()
    await check('B can ask while A is still generating', () => canAskInB)
    if (!canAskInB) return
    await composer(page).fill(B_QUESTION)
    await composer(page).press('Enter')
    await server.waitForAsk(2)
    await server.finishAsk(1)
    await settle(page)
    await check('A completion does not interrupt B’s question', async () => (await countText(page, B_QUESTION)) === 1)
    await check('A completion does not render in B', async () => (await countText(page, ANSWER)) === 0)
    await check('B remains busy until its own ask completes', async () => !(await composer(page).isEnabled()))
    await server.finishAsk(2)
    await waitFor(() => countText(page, B_ANSWER).then((n) => n > 0), 'B answer visible', 6500)
    await settle(page)
    await check('B has exactly one question', async () => (await countText(page, B_QUESTION)) === 1)
    await check('B has exactly one answer', async () => (await countText(page, B_ANSWER)) === 1)
    await check('B is enabled after its own answer', () => composer(page).isEnabled())
    await select(page, '会话 A', 1, A_HISTORY)
    await answerVisible(page)
    await check('A kept its own answer', async () => (await countText(page, ANSWER)) === 1 && (await countText(page, B_ANSWER)) === 0)
  })

  await runCase('B can complete before A while A remains pending', async ({ page, server, check }) => {
    await open(page)
    await askQuestion(page, server)
    await select(page, '会话 B', 2, B_HISTORY)
    await waitFor(() => composer(page).isEnabled(), 'B composer enabled while A generates')
    await composer(page).fill(B_QUESTION)
    await composer(page).press('Enter')
    await server.waitForAsk(2)
    await server.finishAsk(2)
    await waitFor(() => countText(page, B_ANSWER).then((n) => n > 0), 'B answer visible', 6500)
    await check('B completes with exactly one answer', async () => (await countText(page, B_ANSWER)) === 1)
    await select(page, '会话 A', 1, A_HISTORY)
    await waitFor(() => countText(page, GENERATING).then((n) => n > 0), 'A remains pending after B completes')
    await check('A still has one question and one pending indicator', async () =>
      (await countText(page, QUESTION)) === 1 && (await countText(page, GENERATING)) === 1)
    await check('A remains disabled until its own answer arrives', async () => !(await composer(page).isEnabled()))
    await check('B answer never appears in A', async () => (await countText(page, B_ANSWER)) === 0)
    await server.finishAsk(1)
    await answerVisible(page)
    await check('A completes with exactly one answer', async () => (await countText(page, ANSWER)) === 1)
  })

  await runCase('failed A request does not disturb a draft in B', async ({ page, server, check }) => {
    await open(page)
    await askQuestion(page, server)
    await select(page, '会话 B', 2, B_HISTORY)
    const canEdit = await composer(page).isEnabled()
    await check('B draft is editable while A is pending', () => canEdit)
    if (!canEdit) return
    const draft = '会话 B 中尚未发送的草稿'
    await composer(page).fill(draft)
    await server.dropAsk(1)
    await settle(page)
    await check('A failure leaves B selected', async () => (await page.evaluate(() => location.hash)) === '#/chat/2')
    await check('A failure does not replace B draft', async () => (await composer(page).inputValue()) === draft)
    await check('A failure does not put A question in B', async () => (await countText(page, QUESTION)) === 0)
    await check('A failure does not make B busy', () => composer(page).isEnabled())
  })

  await runCase('return to A while pending; resolve without duplicated messages', async ({ page, server, check }) => {
    await open(page)
    await askQuestion(page, server)
    await select(page, '会话 B', 2, B_HISTORY)
    await select(page, '会话 A', 1, A_HISTORY)
    await waitFor(() => countText(page, GENERATING).then((n) => n > 0), 'pending answer visible')
    await settle(page)
    await check('pending A has exactly one user question', async () => (await countText(page, QUESTION)) === 1)
    await check('pending A has exactly one generating indicator', async () => (await countText(page, GENERATING)) === 1)
    await server.finishAsk(1)
    await answerVisible(page)
    await settle(page)
    await check('completed A has one question', async () => (await countText(page, QUESTION)) === 1)
    await check('completed A has one answer', async () => (await countText(page, ANSWER)) === 1)
    await check('completed A has no generating indicator', async () => (await countText(page, GENERATING)) === 0)
    await check('completed A composer is enabled', () => composer(page).isEnabled())
  })

  await runCase('leave chat and reopen while generating; polling completes the answer', async ({ page, server, check }) => {
    await open(page)
    await askQuestion(page, server)
    await page.locator('#primary-navigation').getByRole('link', { name: '知识库', exact: true }).click()
    await page.waitForFunction(() => location.hash === '#/kbs')
    await page.locator('#primary-navigation').getByRole('link', { name: '智能问答', exact: true }).click()
    await page.locator('.chat-sidebar').getByRole('link', { name: '会话 A' }).waitFor()
    await select(page, '会话 A', 1, A_HISTORY)
    await waitFor(() => countText(page, GENERATING).then((n) => n > 0), 'reopened pending answer visible')
    await check('reopened pending conversation has one question', async () => (await countText(page, QUESTION)) === 1)
    await server.finishAsk(1)
    await answerVisible(page)
    await check('polling renders one completed answer', async () => (await countText(page, ANSWER)) === 1)
    await check('polling stops showing pending on completion', async () => (await countText(page, GENERATING)) === 0)
  })

  await runCase('a delayed pending poll cannot overwrite a completed answer', async ({ page, server, check }) => {
    await open(page)
    await askQuestion(page, server)
    await page.locator('#primary-navigation').getByRole('link', { name: '知识库', exact: true }).click()
    await page.waitForFunction(() => location.hash === '#/kbs')
    await page.locator('#primary-navigation').getByRole('link', { name: '智能问答', exact: true }).click()
    await page.locator('.chat-sidebar').getByRole('link', { name: '会话 A' }).waitFor()
    await select(page, '会话 A', 1, A_HISTORY)
    await waitFor(() => countText(page, GENERATING).then((n) => n > 0), 'reopened pending answer visible')
    const stale = server.holdNextGet(1)
    await waitPromise(stale.arrived.promise, 'pending poll to arrive', 5500)
    await server.finishAsk(1)
    // A serialized polling implementation may wait for the held GET before it
    // can fetch completion. Allow that design, but if completion is already on
    // screen, releasing the older snapshot must never regress it to pending.
    let completedBeforeOldReply = false
    try {
      await waitFor(() => countText(page, ANSWER).then((n) => n > 0), 'later poll to render completed answer', 3000)
      completedBeforeOldReply = true
    } catch { /* Polls may deliberately be serialized. */ }
    stale.release.resolve()
    await waitPromise(stale.responded.promise, 'held poll response to be delivered')
    await settle(page)
    if (completedBeforeOldReply) {
      await check('an older pending snapshot cannot erase a visible answer', async () => (await countText(page, ANSWER)) === 1 && (await countText(page, GENERATING)) === 0)
    }
    await answerVisible(page)
    await check('completed polling has one answer', async () => (await countText(page, ANSWER)) === 1)
    await check('completed polling has no spinner', async () => (await countText(page, GENERATING)) === 0)
  })

  await runCase('out-of-order detail response cannot replace selected conversation', async ({ page, server, check }) => {
    await open(page)
    const slow = server.holdNextGet(2)
    await select(page, '会话 B', 2)
    await waitPromise(slow.arrived.promise, 'held request to arrive')
    await select(page, '会话 A', 1, A_HISTORY)
    slow.release.resolve()
    await waitPromise(slow.responded.promise, 'held detail response to be delivered')
    await settle(page)
    await check('A remains selected after old B detail arrives', async () => (await page.evaluate(() => location.hash)) === '#/chat/1')
    await check('old B detail cannot replace A', async () => (await countText(page, A_HISTORY)) === 1 && (await countText(page, B_HISTORY)) === 0)
  })

  await runCase('completed local answer with failed detail offers retry', async ({ page, server, check }) => {
    await open(page)
    await askQuestion(page, server)
    await select(page, '会话 B', 2, B_HISTORY)
    await server.finishAsk(1)
    const failed = server.failNextGet(1)
    await select(page, '会话 A', 1)
    await waitPromise(failed.responded.promise, 'failed A detail response')
    const retry = page.getByRole('button', { name: /重试/ }).first()
    await waitFor(async () => (await countText(page, ANSWER)) > 0 || await retry.isVisible(), 'fallback answer or retry visible')
    await check('completed local answer remains visible after GET fails', async () => (await countText(page, ANSWER)) === 1)
    const hasRetry = await retry.isVisible()
    await check('failed detail has an explicit retry action', () => hasRetry)
    if (!hasRetry) return
    await retry.click()
    await waitFor(() => composer(page).isEnabled(), 'composer enabled after successful retry')
    await answerVisible(page)
    await check('retry loads one canonical question and answer', async () =>
      (await countText(page, QUESTION)) === 1 && (await countText(page, ANSWER)) === 1)
  })

  await runCase('fresh chat remains available after detail failure without stale KBs', async ({ page, server, check }) => {
    await open(page)
    const failed = server.failNextGet(2)
    await select(page, '会话 B', 2)
    await waitPromise(failed.responded.promise, 'failed B detail response')
    await waitFor(() => page.getByText('模拟会话读取失败', { exact: true }).isVisible(), 'detail failure visible')
    const newButton = page.getByRole('button', { name: '+ 新建会话', exact: true })
    const enabled = await newButton.isEnabled()
    await check('new conversation remains available when detail fails', () => enabled)
    if (!enabled) return
    await newButton.click()
    try {
      await waitFor(async () => (await page.evaluate(() => location.hash)) !== '#/chat/2', 'navigation to a fresh chat', 2500)
    } catch (err) { await check(err.message, () => false); return }
    try {
      await waitFor(() => composer(page).isEnabled(), 'fresh chat composer becomes usable after loading', 5000)
    } catch (err) { await check(err.message, () => false) }
    await settle(page)
    await check('fresh chat does not reuse A’s stale KB selection', () =>
      server.createRequests.every((request) => JSON.stringify(request.body.kb_ids ?? []) !== JSON.stringify([1])))
  }, { differentKbs: true })

  await runCase('delayed B detail cannot send using A knowledge bases', async ({ page, server, check }) => {
    await open(page)
    const aKb = page.locator('.kb-pick').filter({ hasText: '测试知识库' })
    const bKb = page.locator('.kb-pick').filter({ hasText: '会话 B 知识库' })
    await check('A starts with its own knowledge base selected', async () => (await aKb.getAttribute('aria-pressed')) === 'true')
    const slow = server.holdNextGet(2)
    await select(page, '会话 B', 2)
    await waitPromise(slow.arrived.promise, 'B detail GET to arrive')
    if (await composer(page).isEnabled()) {
      await composer(page).fill(B_QUESTION)
      await composer(page).press('Enter')
    }
    await settle(page)
    const earlyAsks = server.askRequests.filter((request) => request.id === 2)
    await check('no early B ask carries A’s knowledge base', () => earlyAsks.every((request) =>
      !((request.body.kb_ids ?? [2]).includes(1))))
    slow.release.resolve()
    await waitPromise(slow.responded.promise, 'B detail response to be delivered')
    if (earlyAsks.length === 0) {
      await thread(page).getByText(B_HISTORY, { exact: true }).waitFor({ timeout: 5000 })
      await waitFor(() => composer(page).isEnabled(), 'B composer enabled after detail loads')
      await check('loaded B selects its own knowledge base', async () =>
        (await aKb.getAttribute('aria-pressed')) === 'false' && (await bKb.getAttribute('aria-pressed')) === 'true')
      await composer(page).fill(B_QUESTION)
      await composer(page).press('Enter')
      await server.waitForAsk(2)
      const sent = server.askRequests.filter((request) => request.id === 2).at(-1)
      await check('B ask uses its own selection after detail loads', () =>
        JSON.stringify(sent.body.kb_ids ?? [2]) === JSON.stringify([2]))
      await server.finishAsk(2)
    }
  }, { differentKbs: true })

  await runCase('delayed B detail cannot patch using A knowledge bases', async ({ page, server, check }) => {
    await open(page)
    const aKb = page.locator('.kb-pick').filter({ hasText: '测试知识库' })
    const bKb = page.locator('.kb-pick').filter({ hasText: '会话 B 知识库' })
    const slow = server.holdNextGet(2)
    await select(page, '会话 B', 2)
    await waitPromise(slow.arrived.promise, 'B detail GET to arrive')
    if (await bKb.isVisible() && await bKb.isEnabled()) await bKb.click()
    await settle(page)
    const earlyPatches = server.patchRequests.filter((request) => request.id === 2)
    await check('no early B selection patch carries A’s knowledge base', () => earlyPatches.every((request) =>
      !((request.body.kb_ids ?? []).includes(1))))
    slow.release.resolve()
    await waitPromise(slow.responded.promise, 'B detail response to be delivered')
    if (earlyPatches.length === 0) {
      await thread(page).getByText(B_HISTORY, { exact: true }).waitFor({ timeout: 5000 })
      await check('loaded B restores its own selection', async () =>
        (await aKb.getAttribute('aria-pressed')) === 'false' && (await bKb.getAttribute('aria-pressed')) === 'true')
    }
  }, { differentKbs: true })

  await runCase('held new chat intent isolates the earlier draft answer', async ({ page, server, check }) => {
    const firstCreate = server.holdCreate()
    const newChatCreate = server.holdCreate()
    const secondQuestionCreate = server.holdCreate()
    await page.goto(`${BASE}/#/chat`, { waitUntil: 'domcontentloaded' })
    await page.locator('.chat-sidebar').getByRole('link', { name: '会话 B' }).waitFor({ timeout: 10000 })
    await waitFor(() => composer(page).isEnabled(), 'initial blank chat composer enabled')
    await composer(page).fill(QUESTION)
    await composer(page).press('Enter')
    await waitPromise(firstCreate.arrived.promise, 'first draft create to arrive')
    const newChat = page.getByRole('button', { name: '+ 新建会话', exact: true })
    const enabled = await newChat.isEnabled()
    await check('new-chat action is available while the first draft is creating', () => enabled)
    if (!enabled) return
    await newChat.click()
    await waitPromise(newChatCreate.arrived.promise, 'new-chat create to arrive')
    firstCreate.release.resolve()
    const firstId = server.createRequests[0].id
    await server.waitForAsk(firstId)
    await settle(page)
    await check('earlier draft question is absent from the unrelated blank chat', async () =>
      (await page.evaluate(() => location.hash)) === '#/chat' && (await countText(page, QUESTION)) === 0)
    const secondCanSubmit = await composer(page).isEnabled()
    await check('new blank chat can submit while the earlier ask is pending', () => secondCanSubmit)
    if (!secondCanSubmit) return
    await composer(page).fill(B_QUESTION)
    await composer(page).press('Enter')
    try {
      await waitPromise(secondQuestionCreate.arrived.promise, 'second question creates its own conversation', 5000)
    } catch (err) { await check(err.message, () => false); return }
    secondQuestionCreate.release.resolve()
    await waitPromise(secondQuestionCreate.responded.promise, 'second question create response')
    const secondId = server.createRequests[2].id
    await server.waitForAsk(secondId)
    await check('second question starts its own ask while the first is pending', () =>
      server.askRequests.some((request) => request.id === secondId && request.body.question === B_QUESTION))
    await settle(page)
    const secondView = await page.evaluate(() => location.hash)
    await server.finishAsk(firstId)
    await settle(page)
    await check('earlier answer does not replace the second question’s view', async () =>
      (await page.evaluate(() => location.hash)) === secondView && (await countText(page, ANSWER)) === 0)
    newChatCreate.release.resolve()
    await waitPromise(newChatCreate.responded.promise, 'new-chat create response')
  })

  await runCase('slow new-conversation creation cannot steal later navigation', async ({ page, server, check }) => {
    const slow = server.holdCreate()
    await page.goto(`${BASE}/#/chat`, { waitUntil: 'domcontentloaded' })
    await page.locator('.chat-sidebar').getByRole('link', { name: '会话 B' }).waitFor({ timeout: 10000 })
    await waitFor(() => composer(page).isEnabled(), 'empty chat composer enabled')
    await composer(page).fill(QUESTION)
    await composer(page).press('Enter')
    await waitPromise(slow.arrived.promise, 'held request to arrive')
    await select(page, '会话 B', 2, B_HISTORY)
    slow.release.resolve()
    // A new conversation should still receive the question even if no longer selected.
    await server.waitForAsk(3)
    await settle(page)
    await check('late create does not navigate away from B', async () => (await page.evaluate(() => location.hash)) === '#/chat/2')
    await check('late create does not overwrite B’s message', async () => (await countText(page, B_HISTORY)) === 1)
    await check('late create does not render its question in B', async () => (await countText(page, QUESTION)) === 0)
    await server.finishAsk(3)
  })

  await runCase('late create cannot navigate a new chat visit after unmount', async ({ page, server, check }) => {
    const slow = server.holdCreate()
    await page.goto(`${BASE}/#/chat`, { waitUntil: 'domcontentloaded' })
    await page.locator('.chat-sidebar').getByRole('link', { name: '会话 B' }).waitFor({ timeout: 10000 })
    await waitFor(() => composer(page).isEnabled(), 'first empty chat composer enabled')
    await composer(page).fill(QUESTION)
    await composer(page).press('Enter')
    await waitPromise(slow.arrived.promise, 'held create request to arrive')
    await page.locator('#primary-navigation').getByRole('link', { name: '知识库', exact: true }).click()
    await page.waitForFunction(() => location.hash === '#/kbs')
    await page.locator('#primary-navigation').getByRole('link', { name: '智能问答', exact: true }).click()
    await page.waitForFunction(() => location.hash === '#/chat')
    await waitFor(() => composer(page).isEnabled(), 'new empty chat composer enabled')
    const draft = '重新打开后尚未发送的新问题'
    await composer(page).fill(draft)
    slow.release.resolve()
    await waitPromise(slow.responded.promise, 'held create response to be delivered')
    await page.waitForTimeout(700)
    await check('old create does not navigate the new /chat visit', async () =>
      (await page.evaluate(() => location.hash)) === '#/chat')
    await check('old create preserves the new visit draft', async () => (await composer(page).inputValue()) === draft)
    await check('old prompt does not appear in the new visit', async () => (await countText(page, QUESTION)) === 0)
  })

  await runCase('delayed draft create failure preserves a newer chat draft', async ({ page, server, check }) => {
    const slow = server.holdCreate()
    await page.goto(`${BASE}/#/chat`, { waitUntil: 'domcontentloaded' })
    await page.locator('.chat-sidebar').getByRole('link', { name: '会话 B' }).waitFor({ timeout: 10000 })
    await waitFor(() => composer(page).isEnabled(), 'first empty chat composer enabled')
    await composer(page).fill(QUESTION)
    await composer(page).press('Enter')
    await waitPromise(slow.arrived.promise, 'held create request to arrive')
    await page.locator('#primary-navigation').getByRole('link', { name: '知识库', exact: true }).click()
    await page.waitForFunction(() => location.hash === '#/kbs')
    await page.locator('#primary-navigation').getByRole('link', { name: '智能问答', exact: true }).click()
    await page.waitForFunction(() => location.hash === '#/chat')
    await waitFor(() => composer(page).isEnabled(), 'new empty chat composer enabled')
    const draft = '返回后正在编辑的新问题'
    await composer(page).fill(draft)
    slow.release.resolve('fail')
    await waitPromise(slow.responded.promise, 'failed create response to be delivered')
    try {
      await waitFor(() => page.getByText('模拟创建会话失败', { exact: true }).first().isVisible(), 'create failure surfaced', 3500)
    } catch (err) {
      const composerText = await page.locator('.chat-composer').innerText()
      await check(`${err.message}; composer text: ${composerText}`, () => false)
    }
    await check('failed old create does not navigate the new visit', async () =>
      (await page.evaluate(() => location.hash)) === '#/chat')
    await check('failed old create preserves the newer draft', async () => (await composer(page).inputValue()) === draft)
    await check('new chat composer stays usable after old create failure', () => composer(page).isEnabled())
  })

  await runCase('multiple failed draft creates recover and dismiss independently', async ({ page, server, check }) => {
    const firstCreate = server.holdCreate()
    const secondCreate = server.holdCreate()
    await page.goto(`${BASE}/#/chat`, { waitUntil: 'domcontentloaded' })
    await page.locator('.chat-sidebar').getByRole('link', { name: '会话 B' }).waitFor({ timeout: 10000 })
    await waitFor(() => composer(page).isEnabled(), 'first blank chat ready')
    await composer(page).fill(QUESTION)
    await composer(page).press('Enter')
    await waitPromise(firstCreate.arrived.promise, 'first draft create')
    await page.locator('#primary-navigation').getByRole('link', { name: '知识库', exact: true }).click()
    await page.waitForFunction(() => location.hash === '#/kbs')
    await page.locator('#primary-navigation').getByRole('link', { name: '智能问答', exact: true }).click()
    await page.waitForFunction(() => location.hash === '#/chat')
    await waitFor(() => composer(page).isEnabled(), 'second blank chat ready')
    await composer(page).fill(B_QUESTION)
    await composer(page).press('Enter')
    await waitPromise(secondCreate.arrived.promise, 'second draft create')
    // Both requests fail before the simulated server inserts a conversation.
    firstCreate.release.resolve('fail')
    await waitPromise(firstCreate.responded.promise, 'first create failure')
    secondCreate.release.resolve('fail')
    await waitPromise(secondCreate.responded.promise, 'second create failure')
    const firstFailure = recoveryGroup(page, QUESTION)
    const secondFailure = recoveryGroup(page, B_QUESTION)
    await waitFor(async () => await firstFailure.isVisible() && await secondFailure.isVisible(),
      'both failed questions shown independently', 5000)
    if (OUT && screenshotPhase === 'recovery') {
      await mkdir(dirname(OUT), { recursive: true })
      await page.screenshot({ path: OUT, animations: 'disabled' })
      console.log(`  Screenshot: ${OUT}`)
    }
    const desktopViewport = page.viewportSize()
    await page.setViewportSize({ width: 390, height: 844 })
    const widths = await page.evaluate(() => ({
      viewport: document.documentElement.clientWidth,
      content: document.documentElement.scrollWidth,
    }))
    await check(`mobile page has no horizontal overflow (${widths.content}/${widths.viewport})`, () =>
      widths.content <= widths.viewport)
    for (const [label, action] of [
      ['first recovery', recoveryAction(firstFailure, QUESTION)],
      ['first dismissal', dismissAction(firstFailure, QUESTION)],
      ['second recovery', recoveryAction(secondFailure, B_QUESTION)],
      ['second dismissal', dismissAction(secondFailure, B_QUESTION)],
    ]) {
      // A trial click scrolls through the queue and checks visibility, enabled
      // state and pointer reachability without changing the pending entries.
      await check(`mobile ${label} action is reachable`, async () => {
        await action.click({ trial: true, timeout: 5000 })
        return true
      })
    }
    if (desktopViewport) await page.setViewportSize(desktopViewport)
    await recoveryAction(firstFailure, QUESTION).click()
    await waitFor(async () => (await composer(page).inputValue()) === QUESTION, 'first failed question restored')
    await check('recovering one question preserves the other entry', () => secondFailure.isVisible())
    await composer(page).press('Enter')
    const retried = await waitFor(() => server.askRequests.find((request) => request.body.question === QUESTION),
      'recovered question submitted')
    await server.finishAsk(retried.id)
    await page.locator('#primary-navigation').getByRole('link', { name: '智能问答', exact: true }).click()
    await page.waitForFunction(() => location.hash === '#/chat')
    await waitFor(() => secondFailure.isVisible(), 'other failed question retained after retry', 5000)
    await check('retrying one failure does not discard the other', () => secondFailure.isVisible())
    await dismissAction(secondFailure, B_QUESTION).click()
    await waitFor(async () => !(await secondFailure.isVisible()), 'other failure dismissed')
    await check('dismissal removes only the selected failure', async () => !(await secondFailure.isVisible()))
  })

  await runCase('unseen failed draft remains recoverable after five minutes', async ({ page, server, check }) => {
    await page.clock.install()
    const slow = server.holdCreate()
    await page.goto(`${BASE}/#/chat`, { waitUntil: 'domcontentloaded' })
    await page.locator('.chat-sidebar').getByRole('link', { name: '会话 B' }).waitFor({ timeout: 10000 })
    await waitFor(() => composer(page).isEnabled(), 'empty chat composer enabled')
    await composer(page).fill(QUESTION)
    await composer(page).press('Enter')
    await waitPromise(slow.arrived.promise, 'held draft create to arrive')
    await page.locator('#primary-navigation').getByRole('link', { name: '知识库', exact: true }).click()
    await page.waitForFunction(() => location.hash === '#/kbs')
    slow.release.resolve('fail')
    await waitPromise(slow.responded.promise, 'failed create response')
    // The user remains away from chat while the failure and its cleanup timer
    // are processed. Advance browser time without waiting five real minutes.
    await pause(300)
    await page.clock.fastForward(301_000)
    await page.locator('#primary-navigation').getByRole('link', { name: '智能问答', exact: true }).click()
    await page.waitForFunction(() => location.hash === '#/chat')
    const group = recoveryGroup(page, QUESTION)
    const questionVisible = async () => (await composer(page).inputValue()) === QUESTION || await group.isVisible()
    try {
      await waitFor(questionVisible, 'unseen failed question recoverable after five minutes', 3500)
    } catch (err) { await check(err.message, () => false) }
    await check('unseen failed question remains available for recovery', questionVisible)
    if ((await composer(page).inputValue()) !== QUESTION && await group.isVisible()) {
      await recoveryAction(group, QUESTION).click()
      await check('recovery restores the unseen question after five minutes', async () =>
        (await composer(page).inputValue()) === QUESTION)
    }
  })

  await runCase('changed login token cannot send old prompt with new token', async ({ page, server, check }) => {
    const slow = server.holdCreate()
    await page.goto(`${BASE}/#/chat`, { waitUntil: 'domcontentloaded' })
    await page.locator('.chat-sidebar').getByRole('link', { name: '会话 B' }).waitFor({ timeout: 10000 })
    await waitFor(() => composer(page).isEnabled(), 'empty chat composer enabled')
    await composer(page).fill(QUESTION)
    await composer(page).press('Enter')
    await waitPromise(slow.arrived.promise, 'held create request to arrive')
    const replacementToken = 'replacement-synthetic-session'
    await page.evaluate((token) => localStorage.setItem('wrk_token', token), replacementToken)
    await check('the replacement token is installed before create completes', async () =>
      (await page.evaluate(() => localStorage.getItem('wrk_token'))) === replacementToken)
    slow.release.resolve()
    await waitPromise(slow.responded.promise, 'held create response to be delivered')
    // A safe implementation may drop the continuation, so allow time for a
    // mistakenly continued ask rather than requiring an ask to occur.
    await page.waitForTimeout(1000)
    const leakedAsks = server.askRequests.filter((request) =>
      request.body.question === QUESTION && request.authorization === `Bearer ${replacementToken}`)
    await check('the old question is never sent under the replacement token', () => leakedAsks.length === 0)
  })

  await runCase('401 ask remains recoverable after same user SPA login', async ({ page, server, check }) => {
    await open(page)
    const timeOrigin = await page.evaluate(() => performance.timeOrigin)
    const rejection = server.rejectNextAsk(1, 401)
    await composer(page).fill(QUESTION)
    await composer(page).press('Enter')
    await waitPromise(rejection.arrived.promise, 'ask to reach server before 401')
    rejection.release.resolve()
    await waitPromise(rejection.responded.promise, '401 ask response')
    await page.waitForFunction(() => location.hash.startsWith('#/login'))
    await page.getByPlaceholder('请输入用户名').fill('tester')
    await page.getByPlaceholder('请输入密码').fill('not-a-real-credential')
    await page.locator('.login-card').getByRole('button', { name: '登录', exact: true }).click()
    await waitFor(async () => !(await page.evaluate(() => location.hash)).startsWith('#/login'), 'same user login')
    await check('login issued a fresh token', async () =>
      (await page.evaluate(() => localStorage.getItem('wrk_token'))) !== 'synthetic-test-token')
    await page.locator('#primary-navigation').getByRole('link', { name: '智能问答', exact: true }).click()
    await page.waitForFunction(() => location.hash === '#/chat')
    await page.locator('.chat-sidebar').getByRole('link', { name: '会话 A' }).waitFor({ timeout: 10000 })
    await select(page, '会话 A', 1, A_HISTORY)
    const group = recoveryGroup(page, QUESTION)
    const recoverable = async () => (await composer(page).inputValue()) === QUESTION || await group.isVisible()
    try {
      await waitFor(recoverable, '401 question remains recoverable for same user', 5000)
    } catch (err) { await check(err.message, () => false) }
    await check('same user can recover the rejected question', recoverable)
    if ((await composer(page).inputValue()) !== QUESTION && await group.isVisible()) {
      await recoveryAction(group, QUESTION).click()
      await check('recovery restores the rejected question', async () => (await composer(page).inputValue()) === QUESTION)
    }
    await check('login and navigation stayed in the same SPA runtime', async () =>
      (await page.evaluate(() => performance.timeOrigin)) === timeOrigin)
  })

  await runCase('401 failed draft is hidden from a different SPA user', async ({ page, server, check }) => {
    await open(page)
    const timeOrigin = await page.evaluate(() => performance.timeOrigin)
    const rejection = server.rejectNextAsk(1, 401)
    await composer(page).fill(QUESTION)
    await composer(page).press('Enter')
    await waitPromise(rejection.arrived.promise, 'ask to reach server before 401')
    rejection.release.resolve()
    await waitPromise(rejection.responded.promise, '401 ask response')
    await page.waitForFunction(() => location.hash.startsWith('#/login'))
    await page.getByPlaceholder('请输入用户名').fill('other')
    await page.getByPlaceholder('请输入密码').fill('not-a-real-credential')
    await page.locator('.login-card').getByRole('button', { name: '登录', exact: true }).click()
    await waitFor(async () => !(await page.evaluate(() => location.hash)).startsWith('#/login'), 'other user login')
    await page.locator('#primary-navigation').getByRole('link', { name: '智能问答', exact: true }).click()
    await page.waitForFunction(() => location.hash === '#/chat')
    await waitFor(() => composer(page).isEnabled(), 'other user blank chat ready')
    await check('the other mocked user is signed in', async () =>
      (await page.locator('#primary-navigation').getByText('另一个用户', { exact: true }).count()) > 0)
    await check('the other user does not see the rejected question', async () =>
      (await composer(page).inputValue()) !== QUESTION &&
      !(await recoveryGroup(page, QUESTION).isVisible()) &&
      (await page.getByText(QUESTION, { exact: true }).count()) === 0)
    await check('other login stayed in the same SPA runtime', async () =>
      (await page.evaluate(() => performance.timeOrigin)) === timeOrigin)
  })

  await runCase('403 ask is recoverable when another KB has the same question', async ({ page, server, check }) => {
    await open(page)
    const rejection = server.rejectNextAsk(1)
    const reconciliation = server.holdNextGet(1)
    await composer(page).fill(QUESTION)
    await composer(page).press('Enter')
    await waitPromise(rejection.arrived.promise, 'rejected ask to arrive')
    await check('local ask used only A’s knowledge base', () =>
      JSON.stringify(server.askRequests[0].body.kb_ids) === JSON.stringify([1]))
    // Simulate a different client committing the same text under a different
    // KB selection before this request fails and reconciliation starts.
    server.insertExternalTurn(1, QUESTION, [2])
    rejection.release.resolve()
    await waitPromise(rejection.responded.promise, '403 ask response')
    await waitPromise(reconciliation.arrived.promise, 'reconciliation GET to arrive')
    reconciliation.release.resolve()
    await waitPromise(reconciliation.responded.promise, 'reconciliation response')
    await thread(page).getByText('其他客户端的回答', { exact: true }).waitFor({ timeout: 5000 })
    const recover = recoveryAction(recoveryGroup(page, QUESTION), QUESTION)
    const recoverable = async () => (await composer(page).inputValue()) === QUESTION || await recover.isVisible()
    try {
      await waitFor(recoverable, 'rejected local question to remain recoverable', 3500)
    } catch (err) { await check(err.message, () => false) }
    await check('other client’s same text does not erase the failed local question', recoverable)
    if ((await composer(page).inputValue()) !== QUESTION && await recover.isVisible()) {
      await recover.click()
      await check('recovery restores the failed local question', async () => (await composer(page).inputValue()) === QUESTION)
    }
  }, { differentKbs: true })

  await runCase('failed ask keeps history visible while reconciliation is held', async ({ page, server, check }) => {
    await open(page)
    await askQuestion(page, server)
    const slow = server.holdNextGet(1)
    await server.dropAsk(1)
    await waitPromise(slow.arrived.promise, 'reconciliation GET to arrive', 5500)
    await settle(page)
    await check('earlier loaded messages remain visible during reconciliation', async () =>
      (await countText(page, A_HISTORY)) === 1)
    await check('composer cannot submit while reconciliation is unresolved', async () =>
      !(await composer(page).isEnabled()))
    slow.release.resolve()
    await waitPromise(slow.responded.promise, 'reconciliation response')
    await waitFor(() => countText(page, GENERATING).then((n) => n > 0), 'reconciled server pending answer')
    server.finishServer(1)
    await answerVisible(page)
  })

  await runCase('lost ask response preserves server pending state and resumes polling', async ({ page, server, check }) => {
    await open(page)
    await askQuestion(page, server)
    await server.dropAsk(1)
    try {
      await waitFor(async () => (await countText(page, QUESTION)) === 1 && (await countText(page, GENERATING)) === 1 && !(await composer(page).isEnabled()), 'server pending restored after response lost', 3500)
    } catch (err) { await check(err.message, () => false) }
    const offlineError = page.locator('.chat-composer').getByText(/后端暂时不可用|请求超时/)
    try {
      await waitFor(async () => (await composer(page).inputValue()) === '' && (await offlineError.count()) === 0,
        'confirmed pending question clears retry draft and error', 2500)
    } catch (err) { await check(err.message, () => false) }
    server.finishServer(1)
    try { await answerVisible(page) } catch (err) { await check(err.message, () => false) }
    await check('lost response eventually shows one answer', async () => (await countText(page, ANSWER)) === 1)
    await check('lost response does not duplicate the question', async () => (await countText(page, QUESTION)) === 1)
    await check('completed canonical answer leaves no misleading question in the composer', async () =>
      (await composer(page).inputValue()) === '')
    await check('completed canonical answer leaves no stale connection error', async () => (await offlineError.count()) === 0)
  })
} finally {
  await browser.close()
}

const failed = results.filter((result) => result.failures.length).length
console.log(`\n${results.length - failed}/${results.length} cases passed`)
if (failed || results.length === 0) process.exitCode = 1

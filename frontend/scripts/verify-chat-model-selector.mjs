/**
 * Verify chat model selection against an already-running Vite server.
 *
 * From frontend/:
 *   node scripts/verify-chat-model-selector.mjs http://127.0.0.1:15372
 * Alternatively set CHAT_TEST_BASE_URL. Add --case=<substring> to run one case.
 * Every /api request is intercepted and answered in memory. The script never
 * starts a server, contacts a real backend, or writes application data.
 */
import { strict as assert } from 'node:assert'
import { chromium } from 'playwright'

const baseArg = process.argv.slice(2).find((arg) => !arg.startsWith('--'))
const BASE = (baseArg || process.env.CHAT_TEST_BASE_URL || 'http://127.0.0.1:15372').replace(/\/+$/, '')
const baseUrl = new URL(BASE)
if (!['http:', 'https:'].includes(baseUrl.protocol) || baseUrl.search || baseUrl.hash) {
  throw new Error('The Vite base URL must be an HTTP(S) URL without a query or fragment')
}
const caseFilter = process.argv.find((arg) => arg.startsWith('--case='))?.slice('--case='.length).toLowerCase()
const TOKEN = 'synthetic-model-selector-session'
const SESSION_CHANGED = '登录状态已更改，请重新登录后恢复提问。'
const MODEL_ERROR = 'selected model unavailable'
const NOW = '2026-01-01T00:00:00Z'
const clone = (value) => JSON.parse(JSON.stringify(value))

function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

async function waitFor(check, description, timeout = 8000) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    if (await check()) return
    await new Promise((resolve) => setTimeout(resolve, 25))
  }
  throw new Error(`Timed out waiting for ${description}`)
}

async function waitForSignal(promise, description, timeout = 8000) {
  let timer
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`Timed out waiting for ${description}`)), timeout) }),
    ])
  } finally {
    clearTimeout(timer)
  }
}

function model(id) {
  return { id, model: `model-${id}`, label: `Model ${id}`, provider: 'Test provider', display: `Model ${id}` }
}

function conversation(id, title) {
  return { id, title, kb_ids: [1], pinned: false, message_count: 0, created_at: NOW, updated_at: NOW, messages: [] }
}

function message(id, role, content) {
  return {
    id, role, content, status: 'ok', kb_ids: [1], created_at: NOW,
    evidence: [], route: { kb_routes: [] }, latency_ms: 0, insufficient: false,
  }
}

function mockServer({ failModels = false } = {}) {
  const conversations = new Map([
    [10, conversation(10, 'Conversation A')],
    [11, conversation(11, 'Conversation B')],
  ])
  const state = {
    defaultId: 1,
    models: [model(1), model(2)],
    failModels,
    modelCalls: 0,
    modelHolds: [],
    createHolds: [],
    allHolds: [],
    creates: [],
    asks: [],
    invalidModels: new Set(),
    matchingTurnOnReject: false,
    unexpected: [],
  }
  let nextConversationId = 12
  let nextMessageId = 1000

  function hold(queue) {
    const gate = { arrived: deferred(), release: deferred() }
    queue.push(gate)
    state.allHolds.push(gate)
    return gate
  }
  state.holdModel = () => hold(state.modelHolds)
  state.holdCreate = () => hold(state.createHolds)
  state.releaseAll = () => state.allHolds.forEach((gate) => gate.release.resolve())

  const reply = (route, data, status = 200) => route.fulfill({
    status, contentType: 'application/json', body: JSON.stringify(data),
  })
  const list = () => [...conversations.values()].map(({ messages, ...item }) => ({
    ...item, message_count: messages.length,
  }))

  async function handle(route) {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    const method = request.method()

    // Serve only Vite's own read-only assets. No non-API writes or external
    // requests are permitted, including when BASE points at another host.
    if (!/^\/api(?:\/|$)/.test(path)) {
      if (url.origin === baseUrl.origin && ['GET', 'HEAD'].includes(method)) return route.continue()
      state.unexpected.push(`${method} ${url.href}`)
      return route.abort()
    }

    if (path === '/api/meta') return reply(route, { version: 'test', needs_setup: false, offline_models: false })
    if (path === '/api/auth/me') return reply(route, {
      user: { id: 1, username: 'tester', display_name: 'Tester', role: 'admin', must_change_password: false },
      permissions: { is_admin: true, kb_grants: [] },
    })
    if (path === '/api/setup/org') return reply(route, { org_name: 'Test organization' })
    if (path === '/api/kbs') return reply(route, {
      items: [{ id: 1, name: 'Test knowledge base', mode: 'basic', mode_label: 'basic', my_level: 'manage' }],
      total: 1,
    })
    if (path === '/api/chat/models' && method === 'GET') {
      state.modelCalls += 1
      const snapshot = { items: clone(state.models), default_id: state.defaultId }
      const fail = state.failModels
      const gate = state.modelHolds.shift()
      if (gate) {
        gate.arrived.resolve()
        await gate.release.promise
      }
      return fail ? reply(route, { detail: 'temporary model-list failure' }, 503) : reply(route, snapshot)
    }
    if (path === '/api/conversations' && method === 'GET') return reply(route, { items: list(), total: conversations.size })
    if (path === '/api/conversations' && method === 'POST') {
      const body = request.postDataJSON()
      const id = nextConversationId++
      const entry = { id, body: clone(body), authorization: request.headers().authorization ?? '' }
      state.creates.push(entry)
      const gate = state.createHolds.shift()
      if (gate) {
        gate.arrived.resolve()
        await gate.release.promise
      }
      const created = conversation(id, body.title)
      created.kb_ids = body.kb_ids ?? []
      conversations.set(id, created)
      return reply(route, created, 201)
    }

    const match = path.match(/^\/api\/conversations\/(\d+)(?:\/(ask\/stream))?$/)
    if (match) {
      const id = Number(match[1])
      const conv = conversations.get(id)
      if (!conv) return reply(route, { detail: 'conversation not found' }, 404)
      if (method === 'GET' && !match[2]) return reply(route, clone(conv))
      if (method === 'POST' && match[2]) {
        const body = request.postDataJSON()
        state.asks.push({ id, body: clone(body), authorization: request.headers().authorization ?? '' })
        if (state.invalidModels.has(body.model_config_id)) {
          if (state.matchingTurnOnReject) {
            conv.messages.push(message(++nextMessageId, 'user', body.question))
            conv.messages.push(message(++nextMessageId, 'assistant', 'Answer from another tab'))
          }
          return reply(route, { detail: MODEL_ERROR }, 400)
        }
        const userMessage = message(++nextMessageId, 'user', body.question)
        const answer = message(++nextMessageId, 'assistant', 'Mock answer')
        conv.messages.push(userMessage, answer)
        const done = {
          type: 'done', content: answer.content, message: answer, evidence: [], route: { kb_routes: [] },
          usage: { calls: 0, latency_ms: 0 }, insufficient: false, suggestions: [], refusal_reason: '', denied: [],
        }
        return route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify(done)}\n\n` })
      }
      if (method === 'PATCH' && !match[2]) {
        Object.assign(conv, request.postDataJSON())
        return reply(route, conv)
      }
    }
    state.unexpected.push(`${method} ${path}`)
    return reply(route, { detail: 'unexpected mocked request' }, 500)
  }
  return { state, handle }
}

const browser = await chromium.launch({ headless: true })

async function setup({ path = '/chat/10', failModels = false } = {}) {
  const context = await browser.newContext()
  const server = mockServer({ failModels })
  const errors = []
  await context.addInitScript((token) => localStorage.setItem('wrk_token', token), TOKEN)
  await context.route('**/*', server.handle)
  const page = await context.newPage()
  page.setDefaultTimeout(8000)
  page.on('pageerror', (error) => errors.push(error.message))
  await page.goto(`${BASE}/#${path}`, { waitUntil: 'domcontentloaded' })
  await page.bringToFront()
  const picker = page.locator('select[aria-label="选择回答使用的模型"]')
  const question = page.locator('textarea[aria-label="提问输入框"]')
  await picker.waitFor({ state: 'visible' })
  await waitFor(async () => !(await question.isDisabled()), 'chat composer to become ready')
  if (failModels) {
    await page.getByRole('button', { name: '重试模型列表' }).waitFor()
  } else {
    await waitFor(async () => !(await picker.isDisabled()), 'model list to load')
  }
  await page.waitForLoadState('networkidle')
  return { context, page, picker, question, ...server, errors }
}

async function closeTest(test) {
  test.state.releaseAll()
  await test.context.close()
  assert.deepEqual(test.errors, [], 'unexpected browser errors')
  assert.deepEqual(test.state.unexpected, [], 'unexpected or external network requests')
}

async function send(test, text) {
  await test.question.fill(text)
  await test.page.getByRole('button', { name: '发送', exact: true }).click()
}

const cases = [
  ['fresh default and overlapping focus', async () => {
    const test = await setup()
    try {
      assert.equal(await test.picker.inputValue(), '1')
      test.state.defaultId = 2
      const gate = test.state.holdModel()
      await send(test, 'Question using the refreshed default')
      await waitForSignal(gate.arrived.promise, 'held request to arrive')
      assert.equal(test.state.asks.length, 0, 'answer must wait for model refresh')
      await test.page.evaluate(() => window.dispatchEvent(new Event('focus')))
      await test.page.evaluate(() => new Promise((resolve) => requestAnimationFrame(resolve)))
      gate.release.resolve()
      await waitFor(() => test.state.asks.length === 1, 'default-model answer request')
      await waitFor(async () => (await test.picker.inputValue()) === '2', 'refreshed default to be displayed')
      assert.equal(test.state.asks[0].body.model_config_id, 2, 'request must use the displayed fresh default')
    } finally { await closeTest(test) }
  }],
  ['submit during older model refresh', async () => {
    const test = await setup()
    try {
      await test.question.fill('Question submitted while an older refresh is pending')
      const gate = test.state.holdModel()
      await test.page.evaluate(() => window.dispatchEvent(new Event('focus')))
      await waitForSignal(gate.arrived.promise, 'older model refresh to arrive')
      test.state.defaultId = 2
      const submit = test.page.getByRole('button', { name: '发送', exact: true })
      const canSubmitDuringRefresh = !(await submit.isDisabled())
      if (canSubmitDuringRefresh) await submit.click()
      assert.equal(test.state.asks.length, 0, 'answer must wait for model refresh')
      gate.release.resolve()
      if (!canSubmitDuringRefresh) await submit.click()
      await waitFor(() => test.state.asks.length === 1, 'answer after older refresh')
      assert.equal(test.state.asks[0].body.model_config_id, 2, 'submit must use the default current when it was sent')
      await waitFor(async () => (await test.picker.inputValue()) === '2', 'current default to be displayed')
    } finally { await closeTest(test) }
  }],
  ['explicit selection', async () => {
    const test = await setup()
    try {
      await test.picker.selectOption('2')
      await send(test, 'Question using explicit model')
      await waitFor(() => test.state.asks.length === 1, 'explicit-model answer request')
      assert.equal(test.state.asks[0].body.model_config_id, 2)
      assert.equal(await test.picker.inputValue(), '2')
    } finally { await closeTest(test) }
  }],
  ['400 rejection restores draft', async () => {
    const test = await setup()
    try {
      await test.picker.selectOption('2')
      test.state.invalidModels.add(2)
      test.state.models = [model(1)]
      test.state.matchingTurnOnReject = true
      await send(test, 'Same question in another tab')
      await test.page.getByText(MODEL_ERROR).waitFor()
      await waitFor(async () => (await test.picker.inputValue()) === '1', 'removed model to be replaced')
      await waitFor(async () => (await test.question.inputValue()) === 'Same question in another tab', 'rejected draft to be restored')
      assert.equal(test.state.asks[0].body.model_config_id, 2)
      assert.match(await test.page.locator('[aria-label="待处理的提问"]').innerText(), /这次提问未发送/)
      await test.page.getByRole('button', { name: '发送', exact: true }).click()
      await waitFor(() => test.state.asks.length === 2, 'recovered question retry')
      assert.equal(test.state.asks[1].body.model_config_id, 1)
    } finally { await closeTest(test) }
  }],
  ['transient model list failure and retry', async () => {
    const test = await setup({ failModels: true })
    try {
      assert.equal(await test.picker.isDisabled(), true)
      assert.match(await test.picker.textContent(), /模型加载失败/)
      test.state.failModels = false
      await test.page.getByRole('button', { name: '重试模型列表' }).click()
      await waitFor(async () => !(await test.picker.isDisabled()), 'model list retry')
      assert.equal(await test.picker.inputValue(), '1')
      await send(test, 'Question after model list recovery')
      await waitFor(() => test.state.asks.length === 1, 'answer after list recovery')
      assert.equal(test.state.asks[0].body.model_config_id, 1)
    } finally { await closeTest(test) }
  }],
  ['submit when model list refresh fails', async () => {
    const test = await setup()
    try {
      test.state.defaultId = 2
      test.state.failModels = true
      await send(test, 'Question while model refresh fails')
      await waitFor(() => test.state.asks.length === 1, 'answer after failed model refresh')
      assert.equal(test.state.asks[0].body.model_config_id, 1, 'failed refresh must preserve the displayed model')
      assert.equal(await test.picker.inputValue(), '1')
      await test.page.getByRole('button', { name: '重试模型列表' }).waitFor()
    } finally { await closeTest(test) }
  }],
  ['submit when model list was never available', async () => {
    const test = await setup({ failModels: true })
    try {
      await send(test, 'Question without a previously loaded model')
      await waitFor(() => test.state.asks.length === 1, 'answer without an available model list')
      assert.equal('model_config_id' in test.state.asks[0].body, false, 'server must choose when no model was displayed')
    } finally { await closeTest(test) }
  }],
  ['new conversation navigation retains submitted model', async () => {
    const test = await setup({ path: '/chat' })
    try {
      await test.picker.selectOption('2')
      const gate = test.state.holdCreate()
      await send(test, 'Question in a new conversation')
      await waitForSignal(gate.arrived.promise, 'held request to arrive')
      assert.equal(test.state.asks.length, 0, 'answer must wait for conversation creation')
      await test.page.getByRole('link', { name: /Conversation B/ }).click()
      await test.page.waitForFunction(() => window.location.hash === '#/chat/11')
      await test.picker.selectOption('1')
      gate.release.resolve()
      await waitFor(() => test.state.asks.length === 1, 'new-conversation answer request')
      assert.equal(test.state.asks[0].id, 12)
      assert.equal(test.state.asks[0].body.model_config_id, 2, 'in-flight request must retain its submitted model')
      assert.equal(new URL(test.page.url()).hash, '#/chat/11', 'delayed create must not navigate away')
    } finally { await closeTest(test) }
  }],
  ['session changes during model refresh', async () => {
    const test = await setup({ path: '/chat' })
    try {
      const gate = test.state.holdModel()
      await send(test, 'Question before session change')
      await waitForSignal(gate.arrived.promise, 'held request to arrive')
      await test.page.evaluate(() => localStorage.setItem('wrk_token', 'replacement-session'))
      gate.release.resolve()
      await test.page.getByText(SESSION_CHANGED).waitFor()
      assert.equal(test.state.creates.length, 0, 'session change must prevent conversation creation')
      assert.equal(test.state.asks.length, 0, 'session change must prevent answer submission')
    } finally { await closeTest(test) }
  }],
  ['session changes during conversation creation', async () => {
    const test = await setup({ path: '/chat' })
    try {
      await test.picker.selectOption('2')
      const gate = test.state.holdCreate()
      await send(test, 'Question before session changes during create')
      await waitForSignal(gate.arrived.promise, 'held request to arrive')
      assert.equal(test.state.creates[0].authorization, `Bearer ${TOKEN}`)
      await test.page.evaluate(() => localStorage.setItem('wrk_token', 'replacement-session'))
      gate.release.resolve()
      await test.page.getByText(SESSION_CHANGED).waitFor()
      assert.equal(test.state.asks.length, 0, 'created conversation must not be asked with a different session')
    } finally { await closeTest(test) }
  }],
]

try {
  let count = 0
  for (const [name, run] of cases) {
    if (caseFilter && !name.toLowerCase().includes(caseFilter)) continue
    await run()
    count += 1
    console.log(`PASS ${name}`)
  }
  if (!count) throw new Error(`No cases match ${JSON.stringify(caseFilter)}`)
} finally {
  await browser.close()
}

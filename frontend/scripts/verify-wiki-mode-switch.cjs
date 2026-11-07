'use strict'
// Run against the existing local Vite server:
//   node frontend/scripts/verify-wiki-mode-switch.cjs
// Set WRK_TEST_BASE_URL to another loopback Vite server when needed.
// API responses are synthetic and stateful; no real backend or model is contacted.
const assert = require('node:assert/strict')
const { chromium } = require('playwright')

const configuredBase = new URL(process.env.WRK_TEST_BASE_URL || 'http://127.0.0.1:5173')
if (!['http:', 'https:'].includes(configuredBase.protocol) ||
    !['127.0.0.1', 'localhost', '[::1]'].includes(configuredBase.hostname) ||
    configuredBase.pathname !== '/' || configuredBase.search || configuredBase.hash) {
  throw new Error('WRK_TEST_BASE_URL must be a loopback origin')
}
const BASE = configuredBase.origin
const TIMEOUT = 12000
const admin = {
  id: 1, username: 'fixture-admin', display_name: '测试管理员', role: 'admin',
  status: 'active', must_change_password: false, created_at: null, last_login_at: null,
}
const wikiPage = {
  id: 21, kb_id: 1, slug: 'first-page', title: '自动生成页面', summary: '测试页面',
  status: 'published', status_label: '已发布', human_locked: false, parent_slug: '',
  order_index: 0, section_count: 1, current_version_id: 31, updated_at: null,
}
const json = (route, data, status = 200) => route.fulfill({
  status, contentType: 'application/json; charset=utf-8', body: JSON.stringify(data),
})

async function isolated(browser, { existingPage = false } = {}) {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 }, locale: 'zh-CN', serviceWorkers: 'block',
  })
  const page = await context.newPage()
  page.setDefaultTimeout(TIMEOUT)
  page.setDefaultNavigationTimeout(TIMEOUT)
  const unexpected = []
  const pageErrors = []
  const requests = []
  const state = { mode: 'basic', hasPage: existingPage, task: null }
  page.on('pageerror', (error) => pageErrors.push(String(error)))

  function kb() {
    const active = state.task?.status === 'running'
    return {
      id: 1, name: '模式切换测试库', description: '', mode: state.mode,
      mode_label: state.mode === 'wiki' ? 'Wiki 增强' : '基础分块',
      status: active ? 'indexing' : 'ready', status_label: active ? '索引中' : '可用',
      status_detail: active ? '正在生成 Wiki 页面' : '1 个文档 / 3 个索引单元',
      embed_signature: 'fixture:embedding:3', embed_dim: 3, my_level: 'manage',
      wiki_ready: state.hasPage, wiki_ready_at: state.hasPage ? '2026-01-01T00:00:00Z' : null,
      created_at: '2026-01-01T00:00:00Z',
      stats: { doc_count: 1, chunk_count: 3, wiki_page_count: Number(state.hasPage) },
      index: {
        chunk_ready: true, chunk_units: 3, wiki_ready: state.hasPage,
        wiki_units: Number(state.hasPage), pending_tasks: active ? 1 : 0,
        embed_signature: 'fixture:embedding:3', embed_dim: 3,
        active_embed_signature: 'fixture:embedding:3', active_embed_dim: 3,
        signature_mismatch: false,
      },
    }
  }

  function startTask(initial) {
    state.task = {
      id: 91, kind: 'rebuild_wiki', kind_label: initial ? '首次生成 Wiki' : '重建 Wiki', status: 'running',
      status_label: '处理中', stage: '处理中', progress: 5, kb_id: 1,
      kb_name: '模式切换测试库', doc_id: null, doc_name: '', error: '', attempts: 1,
      result: {}, usage: {}, usage_by_phase: {}, created_at: '2026-01-01T00:00:00Z',
      started_at: '2026-01-01T00:00:00Z', finished_at: null,
    }
  }

  function completeTask() {
    assert.equal(state.task?.status, 'running', 'fixture task must be running before completion')
    state.hasPage = true
    state.task = {
      ...state.task, status: 'success', status_label: '成功', stage: '完成', progress: 100,
      result: { created: 1, updated: 0, skipped: 0, sections: 1 },
      finished_at: '2026-01-01T00:00:01Z',
    }
  }

  await context.route('**/*', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()
    if (url.origin !== BASE) {
      unexpected.push(`${method} ${url.href} (external request blocked)`)
      return route.abort('blockedbyclient')
    }
    if (url.pathname !== '/api' && !url.pathname.startsWith('/api/')) return route.continue()
    requests.push({ method, path: url.pathname, search: url.search, body: request.postData() })
    try {
      if (method === 'GET' && url.pathname === '/api/health') return await json(route, { ok: true, version: 'fixture' })
      if (method === 'GET' && url.pathname === '/api/meta') return await json(route, {
        app_name: '企业知识库', version: 'fixture', initialized: true, needs_setup: false,
        offline_models: false, capabilities: { agent_enabled: true },
      })
      if (method === 'GET' && url.pathname === '/api/setup/status') return await json(route, { initialized: true })
      if (method === 'GET' && url.pathname === '/api/setup/org') return await json(route, { org_name: '示例企业' })
      if (method === 'POST' && url.pathname === '/api/auth/login') return await json(route, { token: 'fixture-only', user: admin })
      if (method === 'GET' && url.pathname === '/api/auth/me') return await json(route, { user: admin, permissions: { is_admin: true, kb_grants: [] } })
      if (method === 'GET' && url.pathname === '/api/kbs') return await json(route, { items: [kb()], total: 1 })
      if (method === 'GET' && url.pathname === '/api/kbs/1') return await json(route, kb())
      if (method === 'PATCH' && url.pathname === '/api/kbs/1') {
        const body = request.postDataJSON()
        assert.equal(body.mode, 'wiki', 'mode switch PATCH must select wiki')
        assert.equal(state.mode, 'basic', 'fixture expects a single mode switch')
        state.mode = body.mode
        if (!state.hasPage) startTask(true)
        return await json(route, kb())
      }
      if (method === 'GET' && url.pathname === '/api/tasks' && url.searchParams.get('kb_id') === '1') {
        const items = state.task ? [state.task] : []
        return await json(route, { items, total: items.length })
      }
      if (method === 'POST' && url.pathname === '/api/kbs/1/rebuild') {
        assert.equal(request.postDataJSON().kind, 'wiki')
        assert.equal(state.hasPage, true, 'first generation must not use the manual rebuild API')
        startTask(false)
        return await json(route, { task_id: state.task.id })
      }
      if (method === 'GET' && url.pathname === '/api/kbs/1/wiki/tree') {
        const items = state.hasPage ? [wikiPage] : []
        return await json(route, { items, total: items.length, mode: state.mode, wiki_ready: state.hasPage })
      }
      if (method === 'GET' && url.pathname === '/api/kbs/1/wiki/pages/first-page' && state.hasPage) {
        return await json(route, {
          ...wikiPage,
          version: { id: 31, version_no: 1, origin: 'generated', content_md: '首次生成的 Wiki 正文。' },
          evidence: [], related: [],
        })
      }
      unexpected.push(`${method} ${url.pathname}${url.search}`)
      return await json(route, { detail: 'Unexpected API request blocked by test' }, 599)
    } catch (error) {
      unexpected.push(`${method} ${url.pathname}: ${error.stack || error}`)
      try { await json(route, { detail: 'Fixture error' }, 599) } catch { /* request closed */ }
    }
  })

  async function finish() {
    await context.close()
    assert.deepEqual(unexpected, [], 'all requests must match local synthetic fixtures')
    assert.deepEqual(pageErrors, [], 'no uncaught browser errors')
  }
  return { page, requests, state, completeTask, finish }
}

async function openSettings(page) {
  await page.goto(`${BASE}/#/login`, { waitUntil: 'domcontentloaded' })
  await page.getByPlaceholder('请输入用户名').fill('fixture-admin')
  await page.getByPlaceholder('请输入密码').fill('fixture-only-value')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.locator('.sidebar').waitFor()
  await page.goto(`${BASE}/#/kbs/1?tab=settings`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('heading', { name: '模式切换测试库' }).waitFor()
  await page.getByRole('heading', { name: '索引状态' }).waitFor()
}

const modeField = (page) => page.locator('label.field').filter({ hasText: '检索模式' })
const manualRebuilds = (requests) => requests.filter((r) => r.method === 'POST' && r.path === '/api/kbs/1/rebuild')
const count = (requests, method, path) => requests.filter((r) => r.method === method && r.path === path).length

async function switchToWiki(env) {
  const { page, requests, state } = env
  await modeField(page).getByRole('combobox').selectOption('wiki')
  assert.match(await modeField(page).innerText(), /首次.*自动生成/)
  assert.equal(state.mode, 'basic', 'selection alone must not save the mode')
  assert.equal(manualRebuilds(requests).length, 0)
  await page.getByRole('button', { name: '保存修改' }).click()
  await page.getByText('有任务运行中…').waitFor()
  await page.locator('.task-item').getByText('处理中', { exact: true }).waitFor()
  await page.locator('.task-item .task-kind').getByText('首次生成 Wiki', { exact: true }).waitFor()
  assert.equal(state.mode, 'wiki')
  assert.equal(count(requests, 'PATCH', '/api/kbs/1'), 1)
  assert.equal(await page.getByRole('button', { name: '重建 Wiki', exact: true }).isDisabled(), true)
  assert.equal(manualRebuilds(requests).length, 0, 'first generation must not require manual rebuild')
}

async function settingsPolling(env) {
  const { page, requests, completeTask } = env
  await openSettings(page)
  await switchToWiki(env)
  const readsBeforeCompletion = count(requests, 'GET', '/api/kbs/1')
  completeTask()
  await page.getByText('已就绪 · 1 个页面').waitFor()
  await page.locator('.task-item').getByText('成功', { exact: true }).waitFor()
  assert.equal(await page.getByRole('button', { name: '重建 Wiki', exact: true }).isEnabled(), true)
  assert(count(requests, 'GET', '/api/kbs/1') > readsBeforeCompletion, 'KB status must refresh without manual input')
  assert.equal(manualRebuilds(requests).length, 0)
}

async function wikiPolling(env) {
  const { page, requests, completeTask } = env
  await openSettings(page)
  await switchToWiki(env)
  await page.getByRole('tab', { name: /^Wiki/ }).click()
  await page.getByText('该知识库还没有 Wiki 页面').waitFor()
  const treeReadsBeforeCompletion = count(requests, 'GET', '/api/kbs/1/wiki/tree')
  assert(treeReadsBeforeCompletion > 0, 'initial empty Wiki tree must be loaded')
  completeTask()
  await page.locator('.wiki-tree').getByRole('button', { name: /自动生成页面/ }).waitFor()
  await page.locator('.wiki-doc-head').getByRole('heading', { name: '自动生成页面' }).waitFor()
  assert(count(requests, 'GET', '/api/kbs/1/wiki/tree') > treeReadsBeforeCompletion, 'Wiki tree must refresh after completion')
  assert.equal(manualRebuilds(requests).length, 0)
}

async function existingWiki(env) {
  const { page, requests, state } = env
  await openSettings(page)
  await modeField(page).getByRole('combobox').selectOption('wiki')
  assert.match(await modeField(page).innerText(), /更新现有页面.*(?:可|手动).*重建/)
  await page.getByRole('button', { name: '保存修改' }).click()
  await page.getByText('设置已保存').waitFor()
  await page.getByText('已就绪 · 1 个页面').waitFor()
  const rebuild = page.getByRole('button', { name: '重建 Wiki', exact: true })
  await page.waitForFunction(() => {
    const buttons = Array.from(document.querySelectorAll('button'))
    return buttons.some((button) => button.textContent.trim() === '重建 Wiki' && !button.disabled)
  })
  assert.equal(state.task, null, 'switching an existing Wiki must not automatically queue a rebuild')
  assert.equal(manualRebuilds(requests).length, 0)
  await rebuild.click()
  const dialog = page.getByRole('dialog', { name: '重建 Wiki' })
  await dialog.getByText(/已人工锁定的页面不会被覆盖/).waitFor()
  assert.equal(manualRebuilds(requests).length, 0, 'opening confirmation must not start a rebuild')
  await dialog.getByRole('button', { name: '开始重建' }).click()
  await page.getByText('有任务运行中…').waitFor()
  await page.locator('.task-item .task-kind').getByText('重建 Wiki', { exact: true }).waitFor()
  assert.equal(manualRebuilds(requests).length, 1, 'confirmed manual rebuild must still work')
  assert.equal(await rebuild.isDisabled(), true)
}

;(async () => {
  const browser = await chromium.launch({ headless: true })
  const tests = [
    ['first generation refreshes settings', settingsPolling, {}],
    ['first generation refreshes open Wiki', wikiPolling, {}],
    ['existing Wiki remains a manual rebuild', existingWiki, { existingPage: true }],
  ]
  let failed = 0
  try {
    for (const [name, test, options] of tests) {
      const env = await isolated(browser, options)
      let failure
      try { await test(env) } catch (error) { failure = error }
      try { await env.finish() } catch (error) { failure = failure ? new Error(`${failure.stack || failure}\n${error.stack || error}`) : error }
      if (failure) { failed++; console.error(`FAIL ${name}: ${failure.stack || failure}`) }
      else console.log(`PASS ${name}`)
    }
  } finally { await browser.close() }
  console.log(`${tests.length - failed} passed; ${failed} failed (synthetic API fixtures)`)
  if (failed) process.exitCode = 1
})().catch((error) => { console.error(error.stack || error); process.exitCode = 1 })

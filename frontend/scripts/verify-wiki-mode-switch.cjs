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
const laterWikiPage = {
  ...wikiPage, id: 22, slug: 'later-page', title: '后续生成页面', order_index: 1, current_version_id: 32,
}
const json = (route, data, status = 200) => route.fulfill({
  status, contentType: 'application/json; charset=utf-8', body: JSON.stringify(data),
})
const deferred = () => {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}
async function within(promise, description) {
  let timer
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`Timed out waiting for ${description}`)), TIMEOUT)
    })])
  } finally { clearTimeout(timer) }
}
// Allow React to apply the response before checking whether the old UI survived.
const afterRender = (page) => page.evaluate(() => new Promise((resolve) => {
  requestAnimationFrame(() => requestAnimationFrame(resolve))
}))

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
  const state = {
    mode: 'basic', name: '模式切换测试库', hasPage: existingPage, hasLaterPage: false, task: null,
    failNextKbRead: false, kbReadStatuses: [], statusDetail: null, heldKbRead: null, heldTaskRead: null,
  }
  page.on('pageerror', (error) => pageErrors.push(String(error)))

  const pages = () => [
    ...(state.hasPage ? [wikiPage] : []), ...(state.hasLaterPage ? [laterWikiPage] : []),
  ]

  function kb() {
    const active = state.task?.status === 'running'
    const pageCount = pages().length
    return {
      id: 1, name: state.name, description: '', mode: state.mode,
      mode_label: state.mode === 'wiki' ? 'Wiki 增强' : '基础分块',
      status: active ? 'indexing' : 'ready', status_label: active ? '索引中' : '可用',
      status_detail: state.statusDetail ?? (active ? '正在生成 Wiki 页面' : '1 个文档 / 3 个索引单元'),
      embed_signature: 'fixture:embedding:3', embed_dim: 3, my_level: 'manage',
      wiki_ready: state.hasPage, wiki_ready_at: state.hasPage ? '2026-01-01T00:00:00Z' : null,
      created_at: '2026-01-01T00:00:00Z',
      stats: { doc_count: 1, chunk_count: 3, wiki_page_count: pageCount },
      index: {
        chunk_ready: true, chunk_units: 3, wiki_ready: state.hasPage,
        wiki_units: pageCount, pending_tasks: active ? 1 : 0,
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

  function completeTask({ addLaterPage = false, removePages = false } = {}) {
    assert.equal(state.task?.status, 'running', 'fixture task must be running before completion')
    assert(!(addLaterPage && removePages), 'fixture cannot add and remove pages at the same time')
    state.hasPage = !removePages
    state.hasLaterPage = addLaterPage
    const pageCount = pages().length
    state.task = {
      ...state.task, status: 'success', status_label: '成功', stage: '完成', progress: 100,
      result: { created: pageCount, updated: 0, skipped: 0, sections: pageCount },
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
    if (url.pathname !== '/api' && !url.pathname.startsWith('/api/')) {
      // Vite serves the app and its assets; application data must come from fixtures.
      if (['fetch', 'xhr', 'eventsource'].includes(request.resourceType())) {
        unexpected.push(`${method} ${url.pathname}${url.search} (unexpected non-API request)`)
        return route.abort('blockedbyclient')
      }
      return route.continue()
    }
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
      if (method === 'GET' && url.pathname === '/api/kbs/1') {
        if (state.failNextKbRead) {
          assert.equal(state.task?.status, 'running', '503 must occur while generation is running')
          state.failNextKbRead = false
          state.kbReadStatuses.push(503)
          state.statusDetail = '故障后轮询已恢复'
          return await json(route, { detail: 'Synthetic transient database busy' }, 503)
        }
        const snapshot = kb()
        state.kbReadStatuses.push(200)
        if (state.heldKbRead) {
          assert.equal(state.task?.status, 'running', 'held request must be a running-task poll')
          const held = state.heldKbRead
          state.heldKbRead = null
          held.request = request
          held.started.resolve()
          await held.release.promise
          try { return await json(route, snapshot) } finally { held.sent.resolve() }
        }
        return await json(route, snapshot)
      }
      if (method === 'PATCH' && url.pathname === '/api/kbs/1') {
        const body = request.postDataJSON()
        assert.equal(body.mode, 'wiki', 'mode switch PATCH must select wiki')
        const switching = state.mode === 'basic'
        state.mode = body.mode
        state.name = body.name ?? state.name
        if (switching && !state.hasPage) startTask(true)
        return await json(route, kb())
      }
      if (method === 'GET' && url.pathname === '/api/tasks' && url.searchParams.get('kb_id') === '1') {
        if (state.heldTaskRead && state.mode === 'wiki') {
          const held = state.heldTaskRead
          held.seen.add(url.searchParams.get('status') || '')
          if (held.seen.has('') && held.seen.has('queued,running')) held.started.resolve()
          await held.release.promise
        }
        const status = url.searchParams.get('status')
        const items = state.task && (!status || status.split(',').some((value) => value.trim() === state.task.status))
          ? [state.task] : []
        return await json(route, { items, total: items.length })
      }
      if (method === 'POST' && url.pathname === '/api/kbs/1/rebuild') {
        assert.equal(request.postDataJSON().kind, 'wiki')
        assert.equal(state.hasPage, true, 'first generation must not use the manual rebuild API')
        startTask(false)
        return await json(route, { task_id: state.task.id })
      }
      if (method === 'GET' && url.pathname === '/api/kbs/1/wiki/tree') {
        const items = pages()
        return await json(route, { items, total: items.length, mode: state.mode, wiki_ready: state.hasPage })
      }
      const pageMatch = url.pathname.match(/^\/api\/kbs\/1\/wiki\/pages\/([^/]+)$/)
      const requestedPage = pageMatch && pages().find((item) => item.slug === decodeURIComponent(pageMatch[1]))
      if (method === 'GET' && pageMatch && decodeURIComponent(pageMatch[1]) === wikiPage.slug &&
          state.task?.status === 'success' && !state.hasPage) {
        return await json(route, { detail: 'Wiki 页面不存在' }, 404)
      }
      if (method === 'GET' && requestedPage) {
        const refreshed = requestedPage.slug === 'first-page' && state.hasLaterPage
        return await json(route, {
          ...requestedPage,
          version: {
            id: requestedPage.current_version_id, version_no: 1, origin: 'generated',
            content_md: refreshed ? '生成完成后的 Wiki 正文。' : '首次生成的 Wiki 正文。',
          },
          evidence: refreshed ? [{ doc_name: '新增证据文档', version_no: 1, locator: '第 2 节', quote: '任务完成后的新证据' }] : [],
          related: refreshed ? [laterWikiPage] : [],
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
  const hint = await modeField(page).innerText()
  assert.match(hint, /首次页面在已有分块时保存后自动生成/)
  assert.match(hint, /否则随文档入库生成/)
  assert.match(hint, /未就绪时回退分块/)
  assert.equal(state.mode, 'basic', 'selection alone must not save the mode')
  assert.equal(manualRebuilds(requests).length, 0)
  await page.getByRole('button', { name: '保存修改' }).click()
  await page.getByText('有任务运行中…').waitFor()
  await page.locator('.task-item .task-head').getByText('处理中', { exact: true }).waitFor()
  await page.locator('.task-item .task-kind').getByText('首次生成 Wiki', { exact: true }).waitFor()
  await page.locator('.info-list').getByText('Wiki 增强', { exact: true }).waitFor()
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

async function settingsPollFailure(env) {
  const { page, requests, state } = env
  await openSettings(page)
  await switchToWiki(env)
  const name = page.locator('label.field').filter({ hasText: '知识库名称' }).locator('input')
  const description = page.getByPlaceholder('说明该知识库收录的内容范围')
  const draft = { name: '未保存的知识库名称', description: '未保存的知识库描述' }
  await name.fill(draft.name)
  await description.fill(draft.description)
  await page.getByText('有未保存的修改').waitFor()

  // The busy event fires only after the app has parsed the synthetic 503.
  await page.evaluate(() => {
    window.__fixtureBusySeen = false
    window.addEventListener('wrk:backend-busy', () => { window.__fixtureBusySeen = true }, { once: true })
  })
  const failure = page.waitForResponse((response) =>
    new URL(response.url()).pathname === '/api/kbs/1' && response.request().method() === 'GET' && response.status() === 503)
  state.failNextKbRead = true
  await failure
  await page.waitForFunction(() => window.__fixtureBusySeen === true)
  await afterRender(page)
  assert.equal(await name.count(), 1, 'the name field must remain available after a failed poll')
  assert.equal(await description.count(), 1, 'the description field must remain available after a failed poll')
  assert.equal(await name.inputValue(), draft.name)
  assert.equal(await description.inputValue(), draft.description)

  // A different status detail proves a later successful automatic poll was applied.
  await page.getByText('故障后轮询已恢复').waitFor()
  const failureIndex = state.kbReadStatuses.indexOf(503)
  assert.deepEqual(state.kbReadStatuses.slice(failureIndex, failureIndex + 2), [503, 200])
  assert.equal(state.kbReadStatuses.filter((status) => status === 503).length, 1)
  assert.equal(await name.inputValue(), draft.name)
  assert.equal(await description.inputValue(), draft.description)
  assert.equal(await page.getByRole('button', { name: '保存修改' }).isEnabled(), true)
  assert.equal(count(requests, 'PATCH', '/api/kbs/1'), 1, 'draft must remain unsaved')
}

async function staleSettingsPoll(env) {
  const { page, state } = env
  await openSettings(page)
  await switchToWiki(env)
  const held = { started: deferred(), release: deferred(), sent: deferred() }
  state.name = '较早轮询的名称'
  state.heldKbRead = held
  let started = false
  try {
    await within(held.started.promise, 'the older settings poll')
    started = true
    // Saving a new name starts a foreground reload while the older poll is held.
    const nameField = page.locator('label.field').filter({ hasText: '知识库名称' }).locator('input')
    await nameField.fill('较新轮询的名称')
    await page.getByRole('button', { name: '保存修改' }).click()
    await page.getByRole('heading', { name: '较新轮询的名称' }).waitFor()
    const olderResponse = page.waitForResponse((response) => response.request() === held.request)
    held.release.resolve()
    await (await olderResponse).finished()
    await afterRender(page)
    assert.equal(await page.getByRole('heading', { name: '较早轮询的名称' }).count(), 0, 'an older response must not replace newer KB data')
    assert.equal(await page.getByRole('heading', { name: '较新轮询的名称' }).count(), 1)
  } finally {
    state.heldKbRead = null
    held.release.resolve()
    if (started) await within(held.sent.promise, 'release of the delayed KB response')
  }
}

async function fastWikiCompletion(env) {
  const { page, state, completeTask } = env
  await openSettings(page)
  const held = { started: deferred(), release: deferred(), seen: new Set() }
  state.heldTaskRead = held
  try {
    await modeField(page).getByRole('combobox').selectOption('wiki')
    await page.getByRole('button', { name: '保存修改' }).click()
    await within(held.started.promise, 'recent and active task lists after saving')
    await page.getByRole('tab', { name: /^Wiki/ }).click()
    await page.getByText('暂无可查看的 Wiki 页面').waitFor()
    completeTask()
    held.release.resolve()
    await page.locator('.wiki-tree').getByRole('button', { name: /自动生成页面/ }).waitFor()
  } finally {
    state.heldTaskRead = null
    held.release.resolve()
  }
}

async function wikiPolling(env) {
  const { page, requests, completeTask } = env
  await openSettings(page)
  await switchToWiki(env)
  await page.getByRole('tab', { name: /^Wiki/ }).click()
  await page.getByText('暂无可查看的 Wiki 页面').waitFor()
  const treeReadsBeforeCompletion = count(requests, 'GET', '/api/kbs/1/wiki/tree')
  assert(treeReadsBeforeCompletion > 0, 'initial empty Wiki tree must be loaded')
  completeTask()
  await page.locator('.wiki-tree').getByRole('button', { name: /自动生成页面/ }).waitFor()
  await page.locator('.wiki-doc-head').getByRole('heading', { name: '自动生成页面' }).waitFor()
  assert(count(requests, 'GET', '/api/kbs/1/wiki/tree') > treeReadsBeforeCompletion, 'Wiki tree must refresh after completion')
  assert.equal(manualRebuilds(requests).length, 0)
}

async function partialWikiPolling(env) {
  const { page, requests, state, completeTask } = env
  await openSettings(page)
  await switchToWiki(env)
  // One page has already appeared, but this is still the first generation task.
  state.hasPage = true
  const initialTree = page.waitForResponse((response) =>
    new URL(response.url()).pathname === '/api/kbs/1/wiki/tree' && response.status() === 200)
  await page.getByRole('tab', { name: /^Wiki/ }).click()
  assert.deepEqual((await (await initialTree).json()).items.map((item) => item.slug), ['first-page'])
  assert.equal(state.task?.status, 'running')
  await page.locator('.wiki-doc-head').getByRole('heading', { name: '自动生成页面' }).waitFor()
  await page.getByText('该页面暂无来源依据记录。').waitFor()
  await page.getByText('暂无关联页面。').waitFor()
  await page.getByRole('button', { name: '编辑校对' }).click()
  const summary = page.getByPlaceholder('页面摘要（可选）')
  const body = page.locator('.wiki-doc textarea.code')
  const draftSummary = '尚未保存的页面摘要'
  const draftBody = '尚未保存的 Wiki 正文，任务结束后应保留。'
  await summary.fill(draftSummary)
  await body.fill(draftBody)
  const treeReads = count(requests, 'GET', '/api/kbs/1/wiki/tree')
  const detailReads = count(requests, 'GET', '/api/kbs/1/wiki/pages/first-page')

  completeTask({ addLaterPage: true })
  await page.locator('.wiki-tree').getByRole('button', { name: /后续生成页面/ }).waitFor()
  assert(count(requests, 'GET', '/api/kbs/1/wiki/tree') > treeReads, 'populated tree must refresh after completion')
  assert.equal(await page.locator('.wiki-tree').getByRole('button', { name: /自动生成页面/ }).getAttribute('aria-current'), 'page')
  assert.equal(await summary.inputValue(), draftSummary, 'completion must preserve the open summary draft')
  assert.equal(await body.inputValue(), draftBody, 'completion must preserve the open body draft')

  // Detail may refresh after editing ends, but must include the new evidence and relation.
  await page.locator('.wiki-doc-head').getByRole('button', { name: '取消', exact: true }).click()
  await page.getByText('任务完成后的新证据').waitFor()
  await page.getByRole('button', { name: '后续生成页面', exact: true }).waitFor()
  assert(count(requests, 'GET', '/api/kbs/1/wiki/pages/first-page') > detailReads, 'selected page detail must refresh after completion')
  assert.equal(count(requests, 'PUT', '/api/kbs/1/wiki/pages/first-page'), 0, 'draft must remain unsaved')
  assert.equal(manualRebuilds(requests).length, 0)
}

async function emptyWikiPreservesDraft(env) {
  const { page, requests, state, completeTask } = env
  await openSettings(page)
  await switchToWiki(env)
  state.hasPage = true
  const initialTree = page.waitForResponse((response) =>
    new URL(response.url()).pathname === '/api/kbs/1/wiki/tree' && response.status() === 200)
  await page.getByRole('tab', { name: /^Wiki/ }).click()
  assert.deepEqual((await (await initialTree).json()).items.map((item) => item.slug), ['first-page'])
  assert.equal(state.task?.status, 'running')
  await page.locator('.wiki-doc-head').getByRole('heading', { name: '自动生成页面' }).waitFor()
  await page.getByRole('button', { name: '编辑校对' }).click()
  const summary = page.getByPlaceholder('页面摘要（可选）')
  const body = page.locator('.wiki-doc textarea.code')
  const draftSummary = '目录清空时仍需保留的摘要'
  const draftBody = '目录清空时仍需保留的 Wiki 正文。'
  await summary.fill(draftSummary)
  await body.fill(draftBody)

  const emptyTree = page.waitForResponse(async (response) =>
    new URL(response.url()).pathname === '/api/kbs/1/wiki/tree' && response.status() === 200 &&
    (await response.json()).items.length === 0)
  completeTask({ removePages: true })
  await emptyTree
  await page.locator('.wiki-tree .wiki-tree-item').waitFor({ state: 'detached' })
  assert.equal(await summary.isVisible(), true, 'summary editor must remain visible after the tree becomes empty')
  assert.equal(await body.isVisible(), true, 'body editor must remain visible after the tree becomes empty')
  assert.equal(await summary.inputValue(), draftSummary, 'empty tree must preserve the open summary draft')
  assert.equal(await body.inputValue(), draftBody, 'empty tree must preserve the open body draft')
  assert.equal(await page.getByText('暂无可查看的 Wiki 页面').count(), 0, 'empty state must wait until editing ends')

  await page.locator('.wiki-doc-head').getByRole('button', { name: '取消', exact: true }).click()
  await page.getByText('暂无可查看的 Wiki 页面').waitFor()
  assert.equal(count(requests, 'PUT', '/api/kbs/1/wiki/pages/first-page'), 0, 'cancel must not save the drafts')
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
    ['failed settings poll preserves drafts and recovers', settingsPollFailure, {}],
    ['older settings poll cannot replace newer data', staleSettingsPoll, {}],
    ['fast completion refreshes Wiki without a pending transition', fastWikiCompletion, {}],
    ['first generation refreshes open Wiki', wikiPolling, {}],
    ['partially generated Wiki refreshes without discarding draft', partialWikiPolling, {}],
    ['empty refreshed Wiki preserves draft until cancel', emptyWikiPreservesDraft, {}],
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

'use strict'
// Start the frontend Vite server, then run: node frontend/scripts/verify-ui-workflows.cjs
// Set WRK_TEST_BASE_URL to override http://127.0.0.1:5173; optional arguments select tests.
// All /api requests are intercepted in a fresh browser context; unknown APIs fail closed.
const assert = require('node:assert/strict')
const { chromium } = require('playwright')
const BASE = process.env.WRK_TEST_BASE_URL || 'http://127.0.0.1:5173'
const ORIGIN = new URL(BASE).origin
const TEST_TIMEOUT = 10000

const admin = { id: 1, username: 'fixture-user', display_name: 'Fixture', role: 'admin', status: 'active', created_at: null, last_login_at: null }
const kb = { id: 1, name: 'Fixture knowledge base', description: '', mode: 'wiki', status: 'ready', status_detail: '', mode_label: 'Wiki', status_label: 'ready', embed_signature: '', embed_dim: 0, wiki_ready: true, my_level: 'manage', created_at: null, stats: { doc_count: 0, chunk_count: 0, wiki_page_count: 3 } }
const titles = { alpha: 'Alpha page', beta: 'Beta page', gamma: 'Gamma page' }
const pages = Object.entries(titles).map(([slug, title], i) => ({ id: i + 1, kb_id: 1, slug, title, summary: `Summary ${slug}`, status: 'published', human_locked: false, parent_slug: null, order_index: i, section_count: 1, current_version_id: i + 1, updated_at: null }))
const related = { alpha: ['beta'], beta: ['gamma'], gamma: ['alpha'] }
const detail = (slug) => ({ page: pages.find((p) => p.slug === slug), version: { id: 1, version_no: 1, origin: 'human', content_md: `BODY-${slug}-original` }, evidence: [], related: related[slug].map((key) => pages.find((p) => p.slug === key)) })
const json = (route, data, status = 200) => route.fulfill({ status, contentType: 'application/json; charset=utf-8', body: JSON.stringify(data) })
const deferred = () => { let resolve; const promise = new Promise((r) => { resolve = r }); return { promise, resolve } }
async function within(promise, description) {
  let timer
  try { return await Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`Timed out waiting for ${description}`)), TEST_TIMEOUT) })]) }
  finally { clearTimeout(timer) }
}
const filenames = (request) => Array.from((request.postDataBuffer() || Buffer.alloc(0)).toString('utf8').matchAll(/Content-Disposition: form-data; name="files"; filename="([^"]+)"/gi), (m) => m[1])
const syntheticFile = (name, contents) => ({ name, mimeType: 'text/plain', buffer: Buffer.from(contents) })

async function isolated(browser, options = {}) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN', serviceWorkers: 'block' })
  const page = await context.newPage()
  page.setDefaultTimeout(TEST_TIMEOUT)
  page.setDefaultNavigationTimeout(15000) // Vite may be rebuilding on the first navigation.
  const unexpected = []
  const requests = []
  const pageErrors = []
  page.on('pageerror', (error) => pageErrors.push(String(error)))
  if (options.shortenTimeout) {
    // Trigger the application's own AbortController timeout, without waiting 60 seconds.
    await page.addInitScript(() => {
      const realSetTimeout = window.setTimeout.bind(window)
      window.setTimeout = (callback, delay, ...args) => realSetTimeout(callback, delay === 60000 ? 300 : delay, ...args)
    })
  }
  await context.route('**/*', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname === '/api' || url.pathname.startsWith('/api/')) {
      requests.push({ method: request.method(), pathname: url.pathname, search: url.search })
      try {
        if (options.api && await options.api(route, request, url)) return
        if (url.pathname === '/api/meta') return await json(route, { app_name: 'Fixture', version: '0', initialized: !options.setup, needs_setup: Boolean(options.setup), offline_models: false })
        if (url.pathname === '/api/setup/status') return await json(route, { initialized: false, app_name: 'Fixture', org_name: '', version: '0' })
        if (url.pathname === '/api/setup/org') return await json(route, { org_name: '' })
        if (url.pathname === '/api/auth/login') return await json(route, { token: 'synthetic-browser-token', user: admin })
        if (url.pathname === '/api/auth/me') return await json(route, { user: admin, permissions: { is_admin: true, kb_grants: [] } })
        if (url.pathname === '/api/kbs' && request.method() === 'GET') return await json(route, { items: [kb], total: 1 })
        if (url.pathname === '/api/kbs/1' && request.method() === 'GET') return await json(route, kb)
        if (url.pathname === '/api/kbs/1/documents' && request.method() === 'GET') return await json(route, { items: [], total: 0 })
        if (url.pathname === '/api/tasks' && request.method() === 'GET') return await json(route, { items: [], total: 0 })
        if (url.pathname === '/api/kbs/1/wiki/tree') return await json(route, { items: pages })
        const match = url.pathname.match(/^\/api\/kbs\/1\/wiki\/pages\/([^/]+)$/)
        if (match && request.method() === 'GET' && titles[decodeURIComponent(match[1])]) return await json(route, detail(decodeURIComponent(match[1])))
        unexpected.push(`${request.method()} ${url.pathname}${url.search}`)
        return await json(route, { detail: 'Unexpected API request blocked by test' }, 599)
      } catch (error) {
        // An aborted or closed page may already have canceled its held request.
        if (!/Target page, context or browser has been closed|Request is already handled|Route is already handled|Protocol error|Target closed/i.test(String(error))) throw error
      }
    }
    if (url.origin !== ORIGIN) return route.abort('blockedbyclient')
    return route.continue()
  })
  async function finish() {
    await context.close()
    assert.deepEqual(unexpected, [], 'all API traffic must match synthetic fixtures')
    assert.deepEqual(pageErrors, [], 'no uncaught browser errors')
  }
  return { page, context, requests, unexpected, finish }
}

async function loginAndOpen(page, tab) {
  await page.goto(`${BASE}/#/login`, { waitUntil: 'domcontentloaded' })
  await page.getByPlaceholder('请输入用户名').fill('fixture-user')
  await page.getByPlaceholder('请输入密码').fill('fixture-only-value')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.locator('.sidebar').waitFor()
  await page.goto(`${BASE}/#/kbs/1?tab=${tab}`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('heading', { name: 'Fixture knowledge base' }).waitFor()
}
async function openDocs(page) {
  await loginAndOpen(page, 'documents')
  await page.getByRole('button', { name: '选择文件上传' }).waitFor()
}
async function openWiki(page) {
  await loginAndOpen(page, 'wiki')
  await page.locator('.wiki-doc-head').getByRole('heading', { name: 'Alpha page' }).waitFor()
}
const queue = (page) => page.locator('.upload-list')
async function pick(page, files) { await page.locator('input[type=file][multiple]').setInputFiles(files) }
async function upload(page) { await page.getByRole('button', { name: /上传\s*\d+\s*个文件/ }).click() }
async function queueContains(page, name, present = true) {
  await page.waitForFunction(({ name, present }) => {
    const box = document.querySelector('.upload-list')
    return Boolean(box && box.textContent.includes(name)) === present
  }, { name, present }, { timeout: TEST_TIMEOUT })
}
const treeItem = (page, name) => page.locator('.wiki-tree').getByRole('button', { name: new RegExp(name) })
async function wikiHeading(page, name) { await page.locator('.wiki-doc-head').getByRole('heading', { name }).waitFor() }

async function setup(browser) {
  let posts = 0
  const env = await isolated(browser, { setup: true, api: async (route, request, url) => {
    if (url.pathname === '/api/setup/admin' && request.method() === 'POST') { posts++; await json(route, { detail: 'Synthetic setup validation response' }, 400); return true }
    return false
  } })
  try {
    const { page } = env
    await page.goto(`${BASE}/#/setup`, { waitUntil: 'domcontentloaded' })
    await page.getByRole('heading', { name: '初始化系统' }).waitFor()
    const pwd = page.getByPlaceholder('请设置密码')
    const confirm = page.getByPlaceholder('再次输入密码')
    const cases = [
      { name: '10+ lowercase and digits', value: 'abcdefgh12', accepted: false, message: '密码需包含' },
      { name: '10+ lower and upper', value: 'abcdefghIJ', accepted: false, message: '密码需包含' },
      { name: 'short three classes', value: 'Abcdefg12', accepted: false, message: '密码至少 10 位' },
      { name: '10 lower upper digit', value: 'Abcdefgh12', accepted: true, message: 'Synthetic setup validation response' },
      { name: '10 lower digit symbol', value: 'abcdefgh1!', accepted: true, message: 'Synthetic setup validation response' },
    ]
    for (const test of cases) {
      await pwd.fill(test.value); await confirm.fill(test.value)
      const before = posts
      await page.getByRole('button', { name: '创建管理员并进入系统' }).click()
      await page.locator('.login-error').getByText(test.message, { exact: false }).waitFor()
      assert.equal(posts - before, Number(test.accepted), test.name)
    }
    assert.match((await page.locator('.field-hint').allTextContents()).join(' '), /至少三类/)
  } finally { await env.finish() }
}

async function mixedAndInflight(browser) {
  const firstSeen = deferred(), releaseFirst = deferred()
  const posted = []
  const env = await isolated(browser, { api: async (route, request, url) => {
    if (url.pathname !== '/api/kbs/1/documents' || request.method() !== 'POST') return false
    posted.push(filenames(request))
    if (posted.length === 1) {
      firstSeen.resolve(); await releaseFirst.promise
      await json(route, { items: [{ doc_id: 11, task_id: 21, filename: 'success.txt' }], errors: [{ filename: 'failed.txt', error: 'Synthetic rejection' }] })
    } else await json(route, { items: posted.at(-1).map((filename, i) => ({ doc_id: 30 + i, task_id: 40 + i, filename })), errors: [] })
    return true
  } })
  try {
    const { page } = env
    await openDocs(page)
    await pick(page, [syntheticFile('success.txt', 'success content'), syntheticFile('failed.txt', 'bad content'), syntheticFile('unreported.txt', 'unknown content')])
    await upload(page); await within(firstSeen.promise, 'the initial upload request')
    assert.deepEqual(posted[0], ['success.txt', 'failed.txt', 'unreported.txt'])
    await pick(page, [syntheticFile('added-later.txt', 'in flight')])
    await queueContains(page, 'added-later.txt')
    releaseFirst.resolve()
    await queueContains(page, 'success.txt', false)
    await queueContains(page, 'failed.txt')
    await queueContains(page, 'unreported.txt')
    await queueContains(page, 'added-later.txt')
    await page.getByText('Synthetic rejection', { exact: false }).first().waitFor()
    await queue(page).getByText(/结果.*(未确认|不确定|未知)/).first().waitFor()
    await upload(page)
    await page.waitForFunction(() => !document.querySelector('.upload-list'), null, { timeout: TEST_TIMEOUT })
    assert.equal(posted.length, 2)
    assert.deepEqual([...posted[1]].sort(), ['added-later.txt', 'failed.txt', 'unreported.txt'])
    assert(!posted[1].includes('success.txt'), 'retry must not resend a known success')
  } finally { releaseFirst.resolve(); await env.finish() }
}

async function sameName(browser) {
  const posted = []
  const env = await isolated(browser, { api: async (route, request, url) => {
    if (url.pathname !== '/api/kbs/1/documents' || request.method() !== 'POST') return false
    posted.push(filenames(request))
    await json(route, { items: [{ doc_id: 11, task_id: 21, filename: 'same.txt' }], errors: [] })
    return true
  } })
  try {
    const { page } = env
    await openDocs(page)
    await pick(page, [syntheticFile('same.txt', 'first'), syntheticFile('same.txt', 'first'), syntheticFile('same.txt', 'second is longer')])
    await page.getByText(/同名|无法.*区分|无法.*确认/).first().waitFor()
    assert.equal(await queue(page).getByText('same.txt', { exact: true }).count(), 2, 'both different files remain visible')
    const submit = page.getByRole('button', { name: /上传\s*\d+\s*个文件/ })
    assert.equal(await submit.isDisabled(), true, 'ambiguous names must block submission')
    assert.equal(posted.length, 0, 'do not send ambiguous names')
    await queue(page).getByRole('button', { name: /移除/ }).first().click()
    assert.equal(await queue(page).getByText('same.txt', { exact: true }).count(), 1)
    assert.equal(await submit.isEnabled(), true, 'resolve the ambiguity by removing one file')
    await upload(page)
    await page.waitForFunction(() => !document.querySelector('.upload-list'), null, { timeout: TEST_TIMEOUT })
    assert.deepEqual(posted, [['same.txt']])
  } finally { await env.finish() }
}

async function timeout(browser) {
  const seen = deferred(), release = deferred()
  let postCount = 0
  const env = await isolated(browser, { shortenTimeout: true, api: async (route, request, url) => {
    if (url.pathname !== '/api/kbs/1/documents' || request.method() !== 'POST') return false
    postCount++; seen.resolve(); await release.promise
    try { await route.abort() } catch { /* fetch was already aborted by the app */ }
    return true
  } })
  try {
    const { page } = env
    await openDocs(page)
    await pick(page, [syntheticFile('uncertain.txt', 'timeout content')])
    await upload(page); await within(seen.promise, 'the intercepted request')
    await page.getByText(/结果.*(未确认|不确定|未知)|无法.*确认|可能.*(已|成功|重复)|确认.*(文档|列表)/).first().waitFor()
    await queueContains(page, 'uncertain.txt')
    assert.equal(postCount, 1, 'must not retry automatically after a timeout')
  } finally { release.resolve(); await env.finish() }
}

async function keyboard(browser) {
  const env = await isolated(browser)
  try {
    const { page } = env
    await openWiki(page)
    const beta = treeItem(page, 'Beta page')
    await beta.focus(); await page.keyboard.press('Space'); await wikiHeading(page, 'Beta page')
    const gamma = treeItem(page, 'Gamma page')
    await gamma.focus(); await page.keyboard.press('Enter'); await wikiHeading(page, 'Gamma page')
    const relatedAlpha = page.locator('.related-chips').getByText('Alpha page', { exact: true })
    await relatedAlpha.focus()
    assert(await relatedAlpha.evaluate((element) => document.activeElement === element), 'related page must be keyboard focusable')
    await page.keyboard.press('Enter'); await wikiHeading(page, 'Alpha page')
    await page.locator('.wiki-main').getByText('BODY-alpha-original').waitFor()
  } finally { await env.finish() }
}

async function stale(browser, staleStatus) {
  const seen = deferred(), release = deferred(), sent = deferred()
  const env = await isolated(browser, { api: async (route, request, url) => {
    if (url.pathname !== '/api/kbs/1/wiki/pages/beta' || request.method() !== 'GET') return false
    seen.resolve(); await release.promise
    await json(route, staleStatus === 200 ? detail('beta') : { detail: 'STALE-BETA-ERROR' }, staleStatus)
    sent.resolve(); return true
  } })
  try {
    const { page } = env
    await openWiki(page)
    await treeItem(page, 'Beta page').click(); await within(seen.promise, 'the intercepted request')
    await treeItem(page, 'Gamma page').click(); await wikiHeading(page, 'Gamma page')
    await page.getByText('BODY-gamma-original').waitFor()
    release.resolve(); await within(sent.promise, 'the delayed Wiki response')
    await page.waitForTimeout(200)
    await wikiHeading(page, 'Gamma page')
    assert.equal(await page.getByText('BODY-beta-original').count(), 0, 'stale success must not replace active detail')
    assert.equal(await page.getByText('STALE-BETA-ERROR').count(), 0, 'stale failure must not replace active detail')
    await page.getByText('BODY-gamma-original').waitFor()
  } finally { release.resolve(); await env.finish() }
}

async function dirtyCancel(browser) {
  const env = await isolated(browser)
  try {
    const { page, requests } = env
    let nativeDialogs = 0, nextDecision = 'dismiss'
    page.on('dialog', async (dialog) => { nativeDialogs++; await (nextDecision === 'accept' ? dialog.accept() : dialog.dismiss()) })
    async function chooseAfterClick(target, decision) {
      const beforeNative = nativeDialogs
      nextDecision = decision
      await target.click()
      if (nativeDialogs === beforeNative) {
        const dialog = page.getByRole('dialog')
        await dialog.waitFor()
        await dialog.getByRole('button', { name: decision === 'accept' ? /放弃|切换|继续|确认/ : '取消' }).click()
      }
    }
    await openWiki(page)
    await page.getByRole('button', { name: '编辑校对' }).click()
    const textarea = page.locator('.wiki-main textarea.code')
    const summary = page.getByPlaceholder('页面摘要（可选）')
    await textarea.fill('UNSAVED draft that must survive cancellation')
    await summary.fill('UNSAVED summary')
    const before = requests.filter((r) => r.pathname === '/api/kbs/1/wiki/pages/beta').length
    await chooseAfterClick(treeItem(page, 'Beta page'), 'dismiss')
    assert.equal(await textarea.inputValue(), 'UNSAVED draft that must survive cancellation')
    assert.equal(await summary.inputValue(), 'UNSAVED summary')
    await wikiHeading(page, 'Alpha page')
    assert.equal(requests.filter((r) => r.pathname === '/api/kbs/1/wiki/pages/beta').length, before, 'canceled tree switch must not fetch or navigate to Beta')
    await chooseAfterClick(page.locator('.related-chips').getByText('Beta page', { exact: true }), 'dismiss')
    assert.equal(await textarea.inputValue(), 'UNSAVED draft that must survive cancellation')
    assert.equal(await summary.inputValue(), 'UNSAVED summary')
    assert.equal(requests.filter((r) => r.pathname === '/api/kbs/1/wiki/pages/beta').length, before, 'canceled related-page switch must not fetch Beta')
    await chooseAfterClick(treeItem(page, 'Beta page'), 'accept')
    await wikiHeading(page, 'Beta page')
    assert(requests.filter((r) => r.pathname === '/api/kbs/1/wiki/pages/beta').length > before, 'confirmed switch must fetch Beta')
  } finally { await env.finish() }
}

const tests = {
  setup,
  'upload-mixed-inflight': mixedAndInflight,
  'upload-same-name': sameName,
  'upload-timeout': timeout,
  'wiki-keyboard-related': keyboard,
  'wiki-stale-success': (browser) => stale(browser, 200),
  'wiki-stale-error': (browser) => stale(browser, 500),
  'wiki-dirty-cancel': dirtyCancel,
}
;(async () => {
  const selected = process.argv.slice(2)
  for (const name of selected) if (!tests[name]) throw new Error(`Unknown test ${name}. Choose: ${Object.keys(tests).join(', ')}`)
  const browser = await chromium.launch({ headless: true })
  let failed = 0
  try {
    for (const [name, test] of Object.entries(tests)) {
      if (selected.length && !selected.includes(name)) continue
      try { await test(browser); console.log(`PASS ${name}`) }
      catch (error) { failed++; console.error(`FAIL ${name}: ${error.stack || error}`) }
    }
  } finally { await browser.close() }
  console.log(`${Object.keys(tests).filter((name) => !selected.length || selected.includes(name)).length - failed} passed; ${failed} failed`)
  if (failed) process.exitCode = 1
})().catch((error) => { console.error(error); process.exitCode = 1 })

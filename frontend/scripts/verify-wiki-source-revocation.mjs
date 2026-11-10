// Run from frontend/: node scripts/verify-wiki-source-revocation.mjs
// Exercises the real WikiTab and API adapter with in-memory responses, not visual evidence.
import { strict as assert } from 'node:assert'
import { createRequire } from 'node:module'
import { resolve } from 'node:path'

const require = createRequire(resolve('package.json'))
const { chromium } = require('playwright')
const esbuild = createRequire(require.resolve('vite'))('esbuild')
const code = (await esbuild.build({
  stdin: {
    contents: `
      import React from 'react';
      import { createRoot } from 'react-dom/client';
      import WikiTab from './src/components/WikiTab';
      let root;
      const render = (options) => {
        const kb = window.__makeKb(options.kbId || 1);
        root.render(<WikiTab kb={kb} isAdmin={options.isAdmin || false}
          canManage={options.canManage || false} initialSlug="alpha"
          onToast={(text) => window.__toasts.push(text)} />);
      };
      window.__mount = (options = {}) => {
        if (root) root.unmount();
        root = createRoot(document.getElementById('root'));
        render(options);
      };
      window.__rerender = render;
    `,
    resolveDir: process.cwd(),
    loader: 'tsx',
  },
  bundle: true,
  format: 'iife',
  platform: 'browser',
  jsx: 'automatic',
  write: false,
})).outputFiles[0].text

const browser = await chromium.launch({ headless: true })
const page = await browser.newPage()
const errors = []
page.on('pageerror', (error) => errors.push(error.message))
await page.route('https://wiki.test/**', (route) => route.fulfill({
  contentType: 'text/html', body: '<!doctype html><html><body><div id="root"></div></body></html>',
}))
await page.goto('https://wiki.test/')
await page.evaluate(() => {
  const nativeSetInterval = window.setInterval.bind(window)
  const nativeClearInterval = window.clearInterval.bind(window)
  const checks = new Map()
  let nextTimer = 100000
  window.setInterval = (fn, delay, ...args) => {
    if (delay !== 5000) return nativeSetInterval(fn, delay, ...args)
    const id = nextTimer++
    checks.set(id, () => fn(...args))
    return id
  }
  window.clearInterval = (id) => {
    if (!checks.delete(id)) nativeClearInterval(id)
  }
  window.__poll = () => { for (const check of checks.values()) check() }
  window.__pollCount = () => checks.size
  window.__makeKb = (id) => ({
    id, name: `库 ${id}`, mode: 'wiki', status: 'ready', my_level: 'manage',
    stats: { doc_count: 0, chunk_count: 0, wiki_page_count: 2 },
  })
  const makePage = (slug, id = 1) => ({
    id: slug === 'alpha' ? 1 : 2, kb_id: id, slug,
    title: id === 1 ? (slug === 'alpha' ? 'ALPHA_SECRET_TITLE' : 'BETA_SECRET_TITLE') : 'OTHER_KB_TITLE',
    summary: slug === 'alpha' ? 'ALPHA_SECRET_SUMMARY' : 'BETA_SECRET_SUMMARY',
    status: 'published', status_label: '已发布', human_locked: false, parent_slug: null,
    current_version_id: 7, order_index: slug === 'alpha' ? 1 : 2, section_count: 1, updated_at: null,
  })
  window.__reset = () => {
    window.__toasts = []
    window.__state = {
      pages: { 1: [makePage('alpha'), makePage('beta')], 2: [makePage('alpha', 2)] },
      treeStatus: 200, pageStatus: {}, versionsStatus: 200, saveStatus: 200,
      requests: [], held: [], holdNext: {}, writes: 0, payloads: [],
      content: { '1:alpha': 'ALPHA_SECRET_BODY', '1:beta': 'BETA_BODY', '2:alpha': 'OTHER_KB_BODY' },
      versions: [{ id: 7, version_no: 1, origin: 'auto', content_md: 'HISTORY_SECRET_BODY' }],
      deleted: [{ id: 12, name: '<img src=x onerror=alert(1)>.txt' }],
    }
  }
  const reply = (body, status = 200) => new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json' },
  })
  window.fetch = async (input, options = {}) => {
    const state = window.__state
    const url = new URL(String(input), location.origin)
    const match = url.pathname.match(/^\/api\/kbs\/(\d+)\/wiki\/(?:tree|pages\/([^/]+)(?:\/(versions))?)$/)
    if (!match) throw new Error(`Unexpected request: ${url.pathname}`)
    const kbId = Number(match[1])
    const slug = match[2] && decodeURIComponent(match[2])
    const method = options.method || 'GET'
    const key = !slug ? 'tree' : match[3] ? `versions:${slug}` : `page:${slug}`
    state.requests.push({ kbId, key, method })
    let status = 200
    let body
    if (method !== 'GET') {
      state.writes += 1
      state.payloads.push(JSON.parse(options.body || '{}'))
      status = state.saveStatus
      body = status === 200 ? { ok: true, version_id: 8, version_no: 2 } : { detail: '版本冲突' }
    } else if (!slug) {
      status = state.treeStatus
      body = status === 200 ? { items: state.pages[kbId] ?? [] } : { detail: '模拟目录错误' }
    } else if (match[3]) {
      status = state.versionsStatus
      body = status === 200 ? { items: state.versions } : { detail: '模拟历史错误' }
    } else {
      status = state.pageStatus[`${kbId}:${slug}`] ?? 200
      const current = (state.pages[kbId] ?? []).find((item) => item.slug === slug) ?? makePage(slug, kbId)
      body = status === 200 ? {
        ...current, version: { id: current.current_version_id, version_no: current.current_version_id === 7 ? 1 : 2, origin: 'auto', content_md: state.content[`${kbId}:${slug}`] },
        evidence: [], related: slug === 'alpha' && kbId === 1 ? [makePage('beta')] : [],
        deleted_generation_sources: state.deleted,
      } : { detail: '模拟详情错误' }
    }
    const snapshot = JSON.parse(JSON.stringify(body))
    const holdKey = state.holdNext[`${method}:${key}`] ? `${method}:${key}` : key
    if (state.holdNext[holdKey]) {
      state.holdNext[holdKey] = false
      await new Promise((done) => state.held.push(done))
    }
    if (status === 'network') throw new TypeError('模拟网络中断')
    return reply(snapshot, status)
  }
})
await page.addScriptTag({ content: code })

const waitForTitle = (title) => page.locator('.wiki-doc-head h2').getByText(title, { exact: true }).waitFor()
const mount = async (options = {}) => {
  await page.evaluate((value) => { window.__reset(); window.__mount(value) }, options)
  await waitForTitle(options.kbId === 2 ? 'OTHER_KB_TITLE' : 'ALPHA_SECRET_TITLE')
}
const poll = async () => {
  await page.evaluate(() => window.__poll())
  await page.waitForTimeout(40)
}
const unavailable = () => page.getByText('该页面暂不可查看', { exact: true }).waitFor()
const noSecret = async (...values) => {
  const html = await page.locator('body').innerHTML()
  for (const value of values) assert.equal(html.includes(value), false, `DOM leaked: ${value}`)
}

try {
  const findings = []
  const check = async (name, fn) => {
    try { await fn() } catch (error) { findings.push(`${name}: ${error.message}`) }
  }
  const advanceVersion = () => page.evaluate(() => {
    const state = window.__state
    Object.assign(state.pages[1][0], { current_version_id: 8, title: 'SAFE_V2_TITLE', summary: 'SAFE_V2_SUMMARY' })
    state.pages[1][1].title = 'SAFE_BETA_TITLE'
    state.content['1:alpha'] = 'SAFE_V2_BODY'
    state.versions = [{ id: 8, version_no: 2, origin: 'auto', content_md: 'SAFE_V2_HISTORY' }]
  })

  await check('current version, historical view and related title refresh', async () => {
    await mount()
    await page.getByRole('button', { name: '版本历史', exact: true }).click()
    await page.getByRole('button', { name: '查看内容', exact: true }).click()
    await page.getByText('HISTORY_SECRET_BODY', { exact: true }).waitFor()
    await advanceVersion()
    await poll()
    await noSecret('ALPHA_SECRET_TITLE', 'ALPHA_SECRET_SUMMARY', 'ALPHA_SECRET_BODY', 'HISTORY_SECRET_BODY', 'BETA_SECRET_TITLE')
    assert.equal(await page.getByText('SAFE_V2_BODY', { exact: true }).count(), 1)
    assert.equal(await page.locator('.related-chips').getByText('SAFE_BETA_TITLE').count(), 1)
  })

  await check('history filtered when current version is unchanged', async () => {
    await mount()
    await page.getByRole('button', { name: '版本历史', exact: true }).click()
    await page.getByRole('button', { name: '查看内容', exact: true }).click()
    await page.evaluate(() => { window.__state.versions = [] })
    await poll()
    await noSecret('HISTORY_SECRET_BODY')
  })

  await check('revoked draft remains hidden after a different version returns', async () => {
    await mount({ canManage: true })
    await page.getByRole('button', { name: '编辑校对' }).click()
    await page.locator('textarea.code').fill('OLD_DRAFT_PRIVATE_CONTENT')
    await page.evaluate(() => {
      window.__state.pages[1] = window.__state.pages[1].filter((item) => item.slug !== 'alpha')
      window.__state.pageStatus['1:alpha'] = 404
    })
    await poll()
    await unavailable()
    await page.evaluate(() => {
      const state = window.__state
      state.pages[1].unshift({ ...state.pages[1][0], id: 1, slug: 'alpha', title: 'SAFE_V2_TITLE', summary: 'SAFE_V2_SUMMARY', current_version_id: 8 })
      state.pageStatus['1:alpha'] = 200
      state.content['1:alpha'] = 'SAFE_V2_BODY'
    })
    await poll()
    await noSecret('OLD_DRAFT_PRIVATE_CONTENT', 'ALPHA_SECRET_BODY')
    assert.equal(await page.locator('textarea.code').count(), 0)
    assert.equal(await page.getByRole('button', { name: '保存', exact: true }).count(), 0)
    assert.equal(await page.evaluate(() => window.__state.writes), 0)
  })

  await check('stale admin role is revalidated against server', async () => {
    await mount({ isAdmin: true, canManage: true })
    await page.evaluate(() => {
      window.__state.pages[1] = window.__state.pages[1].filter((item) => item.slug !== 'alpha')
      window.__state.pageStatus['1:alpha'] = 404
    })
    await poll()
    await noSecret('ALPHA_SECRET_TITLE', 'ALPHA_SECRET_BODY')
  })

  await check('in-flight save freezes both inputs and includes original version', async () => {
    await mount({ canManage: true })
    await page.getByRole('button', { name: '编辑校对' }).click()
    await page.locator('textarea.code').fill('UNSAVED_CONTENT_LONG_ENOUGH')
    await page.evaluate(() => { window.__state.holdNext['PUT:page:alpha'] = true })
    await page.getByRole('button', { name: '保存', exact: true }).click()
    await page.waitForFunction(() => window.__state.held.length > 0)
    assert.equal(await page.locator('textarea.code').isDisabled(), true)
    assert.equal(await page.locator('textarea').first().isDisabled(), true)
    assert.equal(await page.evaluate(() => window.__state.payloads[0].expected_version_id), 7)
    await page.evaluate(() => { window.__state.held.splice(0).forEach((release) => release()) })
  })
  if (findings.length) throw new Error(`Revocation regressions:\n${findings.join('\n')}`)

  // A previously opened page disappears, including its title and open version history.
  await mount()
  await page.getByRole('button', { name: '版本历史', exact: true }).click()
  await page.getByRole('button', { name: '查看内容', exact: true }).click()
  await page.getByText('HISTORY_SECRET_BODY', { exact: true }).waitFor()
  await page.evaluate(() => {
    window.__state.pages[1] = window.__state.pages[1].filter((item) => item.slug !== 'alpha')
    window.__state.pageStatus['1:alpha'] = 404
  })
  await poll()
  await unavailable()
  await noSecret('ALPHA_SECRET_TITLE', 'ALPHA_SECRET_SUMMARY', 'ALPHA_SECRET_BODY', 'HISTORY_SECRET_BODY')
  assert.equal(await page.getByRole('dialog').count(), 0)

  // Another page can disappear from the filtered tree and related links while this page remains visible.
  await mount()
  await page.evaluate(() => { window.__state.pages[1] = window.__state.pages[1].filter((item) => item.slug !== 'beta') })
  await poll()
  await waitForTitle('ALPHA_SECRET_TITLE')
  await noSecret('BETA_SECRET_TITLE', 'BETA_SECRET_SUMMARY')
  assert.equal(await page.getByText('页面待复核', { exact: true }).count(), 0)

  // Preserve an unsaved draft, require confirmation to switch, and restore it after access returns.
  await mount({ canManage: true })
  await page.getByRole('button', { name: '编辑校对' }).click()
  const editor = page.locator('textarea.code')
  await editor.fill('UNSAVED_PRIVATE_DRAFT content remains in memory')
  await page.evaluate(() => {
    window.__state.pages[1] = window.__state.pages[1].filter((item) => item.slug !== 'alpha')
    window.__state.pageStatus['1:alpha'] = 404
  })
  await poll()
  await unavailable()
  await noSecret('ALPHA_SECRET_TITLE', 'ALPHA_SECRET_BODY', 'UNSAVED_PRIVATE_DRAFT')
  await page.locator('.wiki-tree button').filter({ hasText: 'BETA_SECRET_TITLE' }).click()
  await page.getByText('放弃未保存的修改？', { exact: true }).waitFor()
  await page.getByRole('dialog', { name: '放弃未保存的修改？' }).getByRole('button', { name: '取消', exact: true }).click()
  await page.evaluate(() => {
    const state = window.__state
    state.pages[1].unshift({ ...state.pages[1][0], id: 1, slug: 'alpha', title: 'ALPHA_RESTORED_TITLE', summary: 'NEW_SUMMARY', order_index: 1 })
    state.pageStatus['1:alpha'] = 200
    state.content['1:alpha'] = 'NEW_VALID_BODY'
  })
  await poll()
  await waitForTitle('ALPHA_RESTORED_TITLE')
  assert.equal(await page.locator('textarea.code').inputValue(), 'UNSAVED_PRIVATE_DRAFT content remains in memory')
  assert.equal(await page.evaluate(() => window.__state.writes), 0)
  await page.locator('.wiki-tree button').filter({ hasText: 'BETA_SECRET_TITLE' }).click()
  await page.getByText('放弃未保存的修改？', { exact: true }).waitFor()
  await page.getByRole('dialog', { name: '放弃未保存的修改？' }).getByRole('button', { name: '取消', exact: true }).click()

  // Transient failures do not hide or reset the page or the current draft.
  await page.evaluate(() => { window.__state.treeStatus = 500; window.__state.pageStatus['1:alpha'] = 'network' })
  await poll()
  await waitForTitle('ALPHA_RESTORED_TITLE')
  assert.equal(await page.locator('textarea.code').inputValue(), 'UNSAVED_PRIVATE_DRAFT content remains in memory')
  assert.equal(await page.getByText('该页面暂不可查看').count(), 0)

  // A late tree/page denial from an old selection cannot hide a newly selected page.
  await mount()
  await page.evaluate(() => {
    const state = window.__state
    state.pages[1] = state.pages[1].filter((item) => item.slug !== 'alpha')
    state.pageStatus['1:alpha'] = 404
    state.holdNext.tree = true
    state.holdNext['page:alpha'] = true
    window.__poll()
  })
  await page.waitForFunction(() => window.__state.held.length === 2)
  await page.locator('.wiki-tree button').filter({ hasText: 'BETA_SECRET_TITLE' }).click()
  await waitForTitle('BETA_SECRET_TITLE')
  await page.evaluate(() => { window.__state.held.splice(0).forEach((release) => release()) })
  await page.waitForTimeout(40)
  await waitForTitle('BETA_SECRET_TITLE')
  assert.equal(await page.getByText('该页面暂不可查看').count(), 0)

  // A late response from another knowledge base cannot hide the new knowledge base.
  await mount()
  await page.evaluate(() => {
    window.__state.treeStatus = 403
    window.__state.pageStatus['1:alpha'] = 404
    window.__state.holdNext.tree = true
    window.__state.holdNext['page:alpha'] = true
    window.__poll()
  })
  await page.waitForFunction(() => window.__state.held.length === 2)
  await page.evaluate(() => { window.__state.treeStatus = 200; window.__rerender({ kbId: 2 }) })
  await waitForTitle('OTHER_KB_TITLE')
  await page.evaluate(() => { window.__state.held.splice(0).forEach((release) => release()) })
  await page.waitForTimeout(40)
  await waitForTitle('OTHER_KB_TITLE')
  assert.equal(await page.getByText('该页面暂不可查看').count(), 0)

  // A 404 detail revokes even if an inconsistent tree still lists the page.
  await mount()
  await page.evaluate(() => { window.__state.pageStatus['1:alpha'] = 404 })
  await poll()
  await unavailable()
  await noSecret('ALPHA_SECRET_TITLE', 'ALPHA_SECRET_BODY')

  // A 403 tree revokes the selected page and all directory entries.
  await mount()
  await page.evaluate(() => { window.__state.treeStatus = 403 })
  await poll()
  await unavailable()
  await noSecret('ALPHA_SECRET_TITLE', 'ALPHA_SECRET_BODY', 'BETA_SECRET_TITLE')

  // System administrators are revalidated against the server too: a stale admin
  // role must not keep cached hidden content visible. They still see the escaped
  // review notice, and a failing tree/detail request alone does not hide it.
  await mount({ isAdmin: true, canManage: true })
  assert.equal(await page.evaluate(() => window.__pollCount()), 1)
  await page.getByText('页面待复核', { exact: true }).waitFor()
  assert.equal(await page.locator('.insufficient-banner img').count(), 0)
  assert.equal(await page.locator('.insufficient-banner').innerText().then((text) => text.includes('<img src=x onerror=alert(1)>.txt')), true)
  await page.evaluate(() => { window.__state.treeStatus = 403; window.__state.pageStatus['1:alpha'] = 404 })
  await poll()
  await waitForTitle('ALPHA_SECRET_TITLE')

  assert.deepEqual(errors, [])
  console.log('Wiki revocation UI checks passed: tree, detail, history, drafts, restoration, races, failures and admin')
} finally {
  await browser.close()
}

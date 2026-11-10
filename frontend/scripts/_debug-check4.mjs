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
  await mount({ canManage: true })
  console.log('pollCount after employee mount:', await page.evaluate(() => window.__pollCount()))
  await mount({ isAdmin: true, canManage: true })
  console.log('pollCount after admin mount:', await page.evaluate(() => window.__pollCount()))
  await page.evaluate(() => {
    window.__state.pages[1] = window.__state.pages[1].filter((item) => item.slug !== 'alpha')
    window.__state.pageStatus['1:alpha'] = 404
  })
  await poll()
  console.log('pollCount after poll:', await page.evaluate(() => window.__pollCount()))
  console.log('requests:', JSON.stringify(await page.evaluate(() => window.__state.requests)))
  const html = await page.locator('body').innerHTML()
  console.log('has ALPHA_SECRET_TITLE:', html.includes('ALPHA_SECRET_TITLE'))
  console.log('has ALPHA_SECRET_BODY:', html.includes('ALPHA_SECRET_BODY'))
  console.log('body snippet:', html.substring(0, 2500))
} finally {
  await browser.close()
}

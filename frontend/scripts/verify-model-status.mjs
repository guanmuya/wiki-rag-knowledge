// Run from frontend/: node scripts/verify-model-status.mjs
// Bundles the real ModelsPage and API normalizer; stubs only toast and HTTP responses.
import { createRequire } from 'node:module'
import { resolve } from 'node:path'
const require = createRequire(resolve('package.json'))
const { chromium } = require('playwright')
const esbuild = createRequire(require.resolve('vite'))('esbuild')
const code = (await esbuild.build({
  stdin: {
    contents: `import React from 'react'; import {createRoot} from 'react-dom/client'; import Page from './src/pages/ModelsPage'; createRoot(document.getElementById('root')).render(<Page/>);`,
    resolveDir: process.cwd(), loader: 'tsx',
  },
  bundle: true, format: 'iife', platform: 'browser', jsx: 'automatic', write: false,
  plugins: [{ name: 'toast-mock', setup(b) {
    b.onResolve({ filter: /^\.\.\/auth$/ }, () => ({ path: 'toast', namespace: 'mock' }))
    b.onLoad({ filter: /.*/, namespace: 'mock' }, () => ({ contents: 'export function useToast(){return {push(){}}}', loader: 'js' }))
  } }],
})).outputFiles[0].text
const browser = await chromium.launch({ headless: true })
const model = (id, label, extra = {}) => ({
  id, provider_id: 1, provider_name: 'Vendor', purpose: 'embedding', model: `embed-${id}`, label,
  params: {}, is_default: false, enabled: true, embed_dim: 3, embed_signature: 'cached-signature',
  last_test_at: null, last_test_ok: null, last_test_message: '', ...extra,
})
const models = [model(1, 'Default', { is_default: true }), model(2, 'Active'), model(3, 'Disabled', { enabled: false })]
const active = (id, name, signature, offline = false) => ({ model_config_id: id, provider: offline ? '' : 'Vendor', model: name, signature, dim: 3, offline })
const row = (id, name, signature, count, mismatch = false) => ({ kb_id: id, kb_name: name, embed_signature: signature, embed_dim: 3, chunk_count: count, mismatch })
function check(condition, message) { if (!condition) throw new Error(message) }
async function mount(scenario) {
  const page = await browser.newPage()
  const errors = []
  page.on('pageerror', err => errors.push(err.message))
  await page.setContent('<div id="root"></div>')
  await page.evaluate(data => {
    window.__mock = { ...data, statusCalls: 0, patches: [] }
    window.fetch = async (url, options = {}) => {
      const state = window.__mock
      let result
      if (url === '/api/settings/providers') result = { items: [] }
      else if (url === '/api/settings/models' && options.method === 'GET') result = { items: state.models }
      else if (url === '/api/settings/embedding/status') { state.statusCalls++; result = state.status }
      else if (url === '/api/kbs') result = { items: state.kbs || [] }
      else if (url.startsWith('/api/settings/models/') && options.method === 'PATCH') {
        const id = Number(url.split('/').pop()), body = JSON.parse(options.body)
        state.patches.push({ id, body })
        state.models = state.models.map(m => m.id === id ? { ...m, ...body } : m)
        if (state.statusAfterToggle) state.status = state.statusAfterToggle
        result = state.models.find(m => m.id === id)
      } else throw new Error(`Unexpected request: ${options.method} ${url}`)
      return new Response(JSON.stringify(result), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
  }, scenario)
  await page.addScriptTag({ content: code })
  const card = page.locator('.card').filter({ has: page.getByRole('heading', { name: 'Embedding 切换' }) })
  await card.locator('.info-list').waitFor()
  return { page, card, errors }
}
async function view(card) {
  return card.evaluate(el => {
    const details = {}
    for (const dt of el.querySelectorAll('dt')) details[dt.textContent.trim()] = dt.nextElementSibling.textContent.trim()
    const badges = {}
    for (const kb of el.querySelectorAll('.embed-kb')) badges[kb.querySelector('.strong').textContent.trim()] = kb.querySelector('.badge').textContent.trim()
    const select = el.querySelector('select')
    return { details, badges, value: select.value, text: select.selectedOptions[0]?.textContent.trim(), options: [...select.options].map(o => o.value), banner: !!el.querySelector('.insufficient-banner') }
  })
}
try {
  {
    const s = await mount({ models, status: { active: active(2, 'embed-2', 'Vendor:embed-2:3'), has_index: true, kbs: [row(1, 'Empty', 'stale', 0, true), row(2, 'Match', 'Vendor:embed-2:3', 3), row(3, 'Mismatch', 'old', 2, true), row(4, 'Unknown', '', 1)] } })
    const v = await view(s.card)
    check(v.value === '2' && v.text.includes('Active（当前）') && v.details['当前 Embedding'].includes('Active'), 'Backend active model must be selected instead of default')
    check(v.options.includes('1') && !v.options.includes('3'), 'Enabled alternatives should be selectable, disabled excluded')
    check(v.badges.Empty === '无索引' && v.badges.Match === '一致' && v.badges.Mismatch === '需重建' && v.badges.Unknown === '无法判断', 'Unindexed, match, mismatch and unknown badges must differ')
    check(v.details['各库一致性'].includes('存在签名不一致') && v.banner, 'Mismatch should produce aggregate warning')
    await s.card.locator('select').selectOption('1')
    check(await s.page.getByText('将 Embedding 切换为', { exact: false }).count() === 1, 'Alternative selection should open confirmation')
    await s.page.getByRole('button', { name: '取消' }).last().click()
    check((await view(s.card)).value === '2', 'Cancel should keep backend current selection')
    check(s.errors.length === 0, `Unexpected component errors: ${s.errors}`)
    await s.page.close()
    console.log('PASS active selection, index badges, and switch cancellation')
  }
  {
    const s = await mount({ models, status: { active: active(null, 'offline-hash', 'offline-hash:3', true), has_index: false, kbs: [row(1, 'Empty', '', 0)] } })
    const v = await view(s.card)
    check(v.value === '' && v.text.includes('offline-hash') && v.text.includes('离线') && v.details['当前 Embedding'].includes('离线'), 'Offline fallback must be identified and selected')
    check(v.badges.Empty === '无索引' && v.details['各库一致性'] === '暂无索引', 'All-unindexed is not consistent')
    await s.page.close()
    console.log('PASS offline fallback and no-index aggregate')
  }
  {
    const s = await mount({ models, status: { kbs: [], has_index: false } })
    const v = await view(s.card)
    check(v.details['当前 Embedding'] === '未配置' && v.details['当前签名'] === '—' && v.value === '' && v.text === '选择向量模型', 'No active model should not guess a default or signature')
    await s.page.close()
    console.log('PASS no active model or signature')
  }
  {
    const s = await mount({ models, status: { active: active(99, 'backend-only', 'remote:3'), kbs: [], has_index: false } })
    const v = await view(s.card)
    check(v.value === '99' && v.text.includes('backend-only') && v.details['当前 Embedding'].includes('backend-only'), 'Missing active list entry needs matching option and backend name')
    await s.page.close()
    console.log('PASS active model missing from model list')
  }
  {
    const s = await mount({ models, status: { active: active(1, 'embed-1', ''), kbs: [row(1, 'Signed', 'known-signature', 4, true)], has_index: true } })
    const v = await view(s.card)
    check(v.details['当前签名'] === '—' && v.badges.Signed === '无法判断' && v.details['各库一致性'] === '无法判断' && !v.banner, 'Missing active signature must remain unknown, without rebuild warning')
    await s.page.close()
    console.log('PASS unknown active signature')
  }
  {
    const s = await mount({ models, status: { active: active(2, 'embed-2', 'sig'), kbs: [], has_index: true }, kbs: [
      { id: 1, name: 'Fallback empty', embed_signature: '', embed_dim: 0, stats: { chunk_count: 0 } },
      { id: 2, name: 'Fallback match', embed_signature: 'sig', embed_dim: 3, stats: { chunk_count: 2 } },
      { id: 3, name: 'Fallback unknown', embed_signature: '', embed_dim: 0, stats: { chunk_count: 1 } },
    ] })
    const v = await view(s.card)
    check(v.badges['Fallback empty'] === '无索引' && v.badges['Fallback match'] === '一致' && v.badges['Fallback unknown'] === '无法判断' && v.details['各库一致性'] === '无法判断', 'Fallback list should drive badges and aggregate')
    await s.page.close()
    console.log('PASS fallback knowledge base list')
  }
  {
    const s = await mount({ models, status: { active: active(1, 'embed-1', 'Vendor:embed-1:3'), kbs: [], has_index: false }, statusAfterToggle: { active: active(null, 'offline-hash', 'offline-hash:3', true), kbs: [], has_index: false } })
    await s.page.getByRole('checkbox', { name: '启用 Default' }).click()
    await s.page.waitForFunction(() => window.__mock.statusCalls >= 2 && [...document.querySelectorAll('.info-list dd')].some(e => e.textContent.includes('offline-hash')))
    const mock = await s.page.evaluate(() => window.__mock), v = await view(s.card)
    check(mock.patches.length === 1 && mock.patches[0].id === 1 && mock.patches[0].body.enabled === false, 'Toggle payload should remain unchanged')
    check(v.value === '' && v.text.includes('离线'), 'Toggle must refresh active status')
    await s.page.close()
    console.log('PASS toggle status refresh and unchanged PATCH')
  }
} finally { await browser.close() }

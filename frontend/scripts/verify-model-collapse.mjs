// Run from frontend/: node scripts/verify-model-collapse.mjs
// Mounts the real ModelsPage with stubbed HTTP and checks collapsible model groups.
import { createRequire } from 'node:module'
import { createServer } from 'node:http'
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

const model = (id, purpose, extra = {}) => ({
  id, provider_id: 1, provider_name: 'Vendor', purpose, purpose_label: purpose,
  model: `${purpose}-${id}`, label: '', params: {}, is_default: false, enabled: true,
  embed_dim: purpose === 'embedding' ? 3 : 0, embed_signature: '', last_test_at: null,
  last_test_ok: null, last_test_message: '', ...extra,
})
const models = [
  ...Array.from({ length: 48 }, (_, i) => model(i + 1, 'llm', { is_default: i === 0 })),
  model(49, 'embedding', { is_default: true }), model(50, 'embedding'),
  model(51, 'vision', { is_default: true }),
]
function check(condition, message) { if (!condition) throw new Error(message) }

// A real HTTP origin keeps localStorage available for the persistence check.
const server = createServer((_req, res) => {
  res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' })
  res.end('<!doctype html><html><body><div id="root"></div></body></html>')
})
await new Promise((done) => server.listen(0, '127.0.0.1', done))
const browser = await chromium.launch({ headless: true })
const page = await browser.newPage()
const errors = []
page.on('pageerror', (err) => errors.push(err.message))
await page.goto(`http://127.0.0.1:${server.address().port}`)

async function mount() {
  await page.setContent('<div id="root"></div>')
  await page.evaluate((data) => {
    window.fetch = async (url) => {
      let result
      if (url === '/api/settings/providers') result = { items: [] }
      else if (url === '/api/settings/models') result = { items: data }
      else if (url === '/api/settings/embedding/status') result = { kbs: [], has_index: false }
      else if (url === '/api/kbs') result = { items: [] }
      else throw new Error(`Unexpected request: ${url}`)
      return new Response(JSON.stringify(result), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
  }, models)
  await page.addScriptTag({ content: code })
  await page.getByText('51 个', { exact: true }).waitFor()
}

const group = (i) => page.locator('.model-group').nth(i)
const toggle = (i) => group(i).locator('.model-group-toggle')
const rows = (i) => group(i).locator('tbody tr')
const visible = async (i) => rows(i).first().isVisible()
const stored = () => page.evaluate(() => localStorage.getItem('wrk_model_groups_collapsed'))

try {
  await mount()
  check(await page.locator('.model-group').count() === 3, 'three purpose groups should render')
  check(await rows(0).count() === 48 && await visible(0), '48 conversation rows should start expanded')
  check(await toggle(0).getAttribute('aria-expanded') === 'true', 'expanded group should report aria-expanded=true')
  check((await toggle(0).innerText()).includes('48'), 'group header should keep the model count')

  await toggle(0).click()
  check(!(await visible(0)), 'conversation rows should hide after collapsing')
  check(await toggle(0).getAttribute('aria-expanded') === 'false', 'collapsed group should report aria-expanded=false')
  check(await visible(1) && await visible(2), 'collapsing one group must not affect the others')
  check(JSON.parse(await stored()).join() === 'llm', 'collapsed purpose should persist to localStorage')

  await mount()
  check(!(await visible(0)), 'collapse state should survive a remount')
  check(await toggle(0).getAttribute('aria-expanded') === 'false', 'remounted toggle should stay collapsed')
  await toggle(0).click()
  check(await rows(0).count() === 48 && await visible(0), 're-expanding should restore all rows')
  check(JSON.parse(await stored()).length === 0, 'expanded state should clear localStorage')

  await page.evaluate(() => localStorage.setItem('wrk_model_groups_collapsed', 'not-json'))
  await mount()
  check(await visible(0), 'malformed stored state should fall back to expanded')
  await toggle(1).click()
  await toggle(2).click()
  check(!(await visible(1)) && !(await visible(2)) && await visible(0), 'groups should collapse independently')
  check(JSON.parse(await stored()).join() === 'embedding,vision', 'multiple collapsed purposes should persist')
  check(errors.length === 0, `unexpected page errors: ${errors}`)
  console.log('PASS model group collapse, persistence, malformed state and independent groups')
} finally {
  await browser.close()
  server.close()
}

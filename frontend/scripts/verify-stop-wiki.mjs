// Run from frontend/: node scripts/verify-stop-wiki.mjs
// Mounts the real task views with an in-memory API; no real backend is contacted.
import { strict as assert } from 'node:assert'
import { createServer } from 'node:http'
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
      import DocumentsTab from './src/components/DocumentsTab';
      import SettingsTab from './src/components/SettingsTab';
      import TasksPage from './src/pages/TasksPage';
      let root;
      const toast = (text, kind) => window.__toasts.push({ text, kind });
      window.__mount = (view, manage) => {
        if (root) root.unmount();
        root = createRoot(document.getElementById('root'));
        const kb = window.__state.kbs[0];
        root.render(view === 'documents'
          ? <DocumentsTab kb={kb} canManage={manage} onChanged={() => {}} onToast={toast} />
          : view === 'settings'
            ? <SettingsTab kb={kb} isAdmin={manage} canManage={manage} onChanged={() => {}} onToast={toast} />
            : <TasksPage />);
      };
    `,
    resolveDir: process.cwd(),
    loader: 'tsx',
  },
  bundle: true,
  format: 'iife',
  platform: 'browser',
  jsx: 'automatic',
  write: false,
  plugins: [{
    name: 'toast-mock',
    setup(build) {
      build.onResolve({ filter: /^\.\.\/auth$/ }, () => ({ path: 'auth', namespace: 'mock' }))
      build.onLoad({ filter: /.*/, namespace: 'mock' }, () => ({
        contents: 'export function useToast(){return {push:(text,kind)=>window.__toasts.push({text,kind})}}',
        loader: 'js',
      }))
    },
  }],
})).outputFiles[0].text

const server = createServer((_request, response) => {
  response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' })
  response.end('<!doctype html><html><body><div id="root"></div></body></html>')
})
await new Promise((done) => server.listen(0, '127.0.0.1', done))
const browser = await chromium.launch({ headless: true })
const page = await browser.newPage()
const errors = []
page.on('pageerror', (error) => errors.push(error.message))
await page.goto(`http://127.0.0.1:${server.address().port}`)
await page.evaluate(() => {
  const kb = (id, level) => ({
    id, name: `测试库 ${id}`, description: '', mode: 'wiki', status: 'ready', status_detail: '',
    mode_label: 'Wiki 增强', status_label: '可用', embed_signature: '', embed_dim: 0,
    wiki_ready: false, my_level: level, created_at: null,
    stats: { doc_count: 0, chunk_count: 0, wiki_page_count: 0 },
  })
  const task = (id, name, fields = {}) => ({
    id, kind: 'ingest_document', kind_label: '文档入库', status: 'running',
    stage: '生成 Wiki 页面', progress: 88, kb_id: 1, kb_name: '测试库 1',
    doc_id: id, doc_name: name, error: '', attempts: 1, result: {},
    run_token: `run-${id}`,
    created_at: null, started_at: null, finished_at: null,
    can_stop_wiki: true, wiki_stop_requested: false, ...fields,
  })
  window.__reset = () => {
    window.__toasts = []
    window.__state = {
      kbs: [kb(1, 'manage'), kb(2, 'read')],
      tasks: [
        task(11, 'ingest-running'),
        task(12, 'queued-rebuild', { kind: 'rebuild_wiki', kind_label: '重建 Wiki', doc_id: null, status: 'queued', run_token: '' }),
        task(13, 'not-wiki', { stage: '解析文档', can_stop_wiki: false }),
        task(14, 'pending-wiki', { can_stop_wiki: false, wiki_stop_requested: true }),
        task(15, 'stopped-wiki', { kind: 'rebuild_wiki', status: 'stopped', can_stop_wiki: false }),
        task(16, 'readonly-wiki', { kb_id: 2, kb_name: '测试库 2' }),
        task(17, 'readonly-stopped', { kb_id: 2, kb_name: '测试库 2', status: 'stopped', can_stop_wiki: false }),
        task(18, 'stopped-reindex', { kind: 'reindex_kb', doc_id: null, status: 'stopped', can_stop_wiki: false }),
        task(19, 'stopped-embedding', { kind: 'embedding_rebuild', doc_id: null, status: 'stopped', can_stop_wiki: false }),
        task(20, 'failed-reindex', { kind: 'reindex_kb', doc_id: null, status: 'failed', can_stop_wiki: false }),
      ],
      requests: [], holdId: null, releaseStop: null, failId: null, failNextGet: false,
      failRefreshAfterStop: false,
    }
  }
  window.__reset()
  const reply = (data, status = 200) => new Response(JSON.stringify(data), {
    status, headers: { 'Content-Type': 'application/json' },
  })
  window.fetch = async (target, options = {}) => {
    const url = new URL(String(target), location.origin)
    const method = options.method || 'GET'
    const state = window.__state
    state.requests.push({ path: url.pathname, method, runToken: url.searchParams.get('run_token') })
    if (url.pathname === '/api/kbs' && method === 'GET') return reply({ items: state.kbs })
    if (url.pathname === '/api/kbs/1/documents' && method === 'GET') return reply({ items: [] })
    if (url.pathname === '/api/tasks' && method === 'GET') {
      if (state.failNextGet) {
        state.failNextGet = false
        return reply({ detail: '模拟刷新失败' }, 500)
      }
      let tasks = state.tasks
      const kbId = url.searchParams.get('kb_id')
      const statuses = url.searchParams.get('status')?.split(',')
      if (kbId) tasks = tasks.filter((item) => item.kb_id === Number(kbId))
      if (statuses) tasks = tasks.filter((item) => statuses.includes(item.status))
      return reply({ items: tasks })
    }
    const stop = url.pathname.match(/^\/api\/tasks\/(\d+)\/stop-wiki$/)
    if (stop && method === 'POST') {
      const id = Number(stop[1])
      if (state.holdId === id) await new Promise((done) => { state.releaseStop = done })
      const selected = state.tasks.find((item) => item.id === id)
      if (!url.searchParams.has('run_token') || url.searchParams.get('run_token') !== selected.run_token) {
        return reply({ detail: '任务已重新执行，请刷新后重试' }, 409)
      }
      if (state.failId === id) {
        // The task may have finished before the failed request reached the server.
        selected.status = 'stopped'
        selected.can_stop_wiki = false
        return reply({ detail: '模拟停止失败' }, 409)
      }
      if (selected.status === 'queued') {
        selected.status = 'stopped'
        selected.can_stop_wiki = false
      } else {
        selected.wiki_stop_requested = true
        selected.can_stop_wiki = false
      }
      if (state.failRefreshAfterStop) state.failNextGet = true
      return reply({ ok: true })
    }
    const retry = url.pathname.match(/^\/api\/tasks\/(\d+)\/retry$/)
    if (retry && method === 'POST') {
      const selected = state.tasks.find((item) => item.id === Number(retry[1]))
      selected.status = 'queued'
      selected.run_token = `retry-${selected.id}`
      selected.wiki_stop_requested = false
      selected.can_stop_wiki = selected.kind === 'rebuild_wiki'
      return reply({ task_id: selected.id, retry_count: 1, reused: true })
    }
    throw new Error(`Unexpected API request: ${method} ${url.pathname}`)
  }
})
await page.addScriptTag({ content: code })

const row = (name) => page.locator('.task-item').filter({ hasText: name })
const mount = async (view, manage = true) => {
  await page.evaluate(({ view, manage }) => {
    window.__reset()
    window.__mount(view, manage)
  }, { view, manage })
  await row('ingest-running').waitFor()
}
const waitForToast = (text) => page.waitForFunction((value) =>
  window.__toasts.some((toast) => toast.text.includes(value)), text)
const requestCount = (path) => page.evaluate((value) =>
  window.__state.requests.filter((request) => request.path === value && request.method === 'POST').length, path)

try {
  await mount('documents')
  assert.equal(await row('not-wiki').getByRole('button', { name: '停止生成' }).count(), 0)
  assert.equal(await row('pending-wiki').getByRole('button', { name: '正在停止…' }).isDisabled(), true)
  await page.evaluate(() => { window.__state.holdId = 11 })
  await row('ingest-running').getByRole('button', { name: '停止生成' }).click()
  await page.waitForFunction(() => Boolean(window.__state.releaseStop))
  assert.equal(await row('ingest-running').getByRole('button', { name: '正在停止…' }).isDisabled(), true)
  assert.equal(await requestCount('/api/tasks/11/stop-wiki'), 1)
  assert.equal(await page.evaluate(() => window.__state.requests.find((request) =>
    request.path === '/api/tasks/11/stop-wiki')?.runToken), 'run-11')
  await page.evaluate(() => window.__state.releaseStop())
  await waitForToast('已发送停止请求')
  await row('ingest-running').getByText('当前步骤完成后停止本次 Wiki 生成', { exact: false }).waitFor()
  assert.equal(await row('ingest-running').getByText('已停止', { exact: true }).count(), 0)
  await page.evaluate(() => {
    const task = window.__state.tasks.find((item) => item.id === 11)
    task.status = 'stopped'
    task.wiki_stop_requested = false
  })
  await page.locator('.card').filter({ has: page.getByRole('heading', { name: '任务列表' }) })
    .getByRole('button', { name: '刷新' }).click()
  await row('ingest-running').getByText('已停止', { exact: true }).waitFor()
  for (const name of ['ingest-running', 'stopped-reindex', 'stopped-embedding']) {
    assert.equal(await row(name).getByRole('button', { name: '重试' }).count(), 0)
    await row(name).getByText('可在知识库设置中重建 Wiki', { exact: false }).waitFor()
  }
  assert.equal(await row('failed-reindex').getByRole('button', { name: '重试' }).count(), 1)
  assert.equal(await requestCount('/api/tasks/11/retry'), 0)

  for (const view of ['documents', 'settings']) {
    await mount(view, false)
    assert.equal(await row('ingest-running').getByRole('button', { name: '停止生成' }).count(), 0)
    assert.equal(await row('pending-wiki').getByRole('button', { name: '正在停止…' }).count(), 0)
    assert.equal(await row('stopped-wiki').getByRole('button', { name: '重试' }).count(), 0)
    await row('pending-wiki').getByText('当前步骤完成后停止本次 Wiki 生成', { exact: false }).waitFor()
  }

  await mount('settings')
  await row('queued-rebuild').getByRole('button', { name: '停止生成' }).click()
  await waitForToast('已发送停止请求')
  await row('queued-rebuild').getByText('已停止', { exact: true }).waitFor()
  assert.equal(await page.evaluate(() => window.__state.requests.find((request) =>
    request.path === '/api/tasks/12/stop-wiki')?.runToken), '')
  assert.equal(await row('queued-rebuild').getByRole('button', { name: '重试' }).count(), 1)
  await row('queued-rebuild').getByRole('button', { name: '重试' }).click()
  assert.equal(await requestCount('/api/tasks/12/retry'), 1)

  // Many newer history entries must not hide an active task on the settings page.
  await page.evaluate(() => {
    window.__reset()
    const template = window.__state.tasks.find((item) => item.id === 15)
    window.__state.tasks.unshift(...Array.from({ length: 10 }, (_, index) => ({
      ...template, id: 100 + index, doc_id: 100 + index, doc_name: `history-${index}`,
    })))
    window.__mount('settings', true)
  })
  await row('ingest-running').getByRole('button', { name: '停止生成' }).waitFor()

  // A successful POST with a failed refresh must not re-enable the stale button.
  await mount('documents')
  await page.evaluate(() => { window.__state.failRefreshAfterStop = true })
  await row('ingest-running').getByRole('button', { name: '停止生成' }).click()
  await waitForToast('已发送停止请求')
  assert.equal(await row('ingest-running').getByRole('button', { name: '正在停止…' }).isDisabled(), true)
  assert.equal(await requestCount('/api/tasks/11/stop-wiki'), 1)
  // If another user has already retried the task, the new run is stoppable again.
  await page.evaluate(() => {
    const task = window.__state.tasks.find((item) => item.id === 11)
    task.status = 'running'
    task.wiki_stop_requested = false
    task.can_stop_wiki = true
    // A different token alone identifies the new run, even if other fields match.
    task.run_token = 'new-run-only'
  })
  await page.locator('.card').filter({ has: page.getByRole('heading', { name: '任务列表' }) })
    .getByRole('button', { name: '刷新' }).click()
  await row('ingest-running').getByRole('button', { name: '停止生成' }).waitFor()

  // A delayed stop for an old run must not disable or stop a retried run.
  await mount('documents')
  await page.evaluate(() => { window.__state.holdId = 11 })
  await row('ingest-running').getByRole('button', { name: '停止生成' }).click()
  await page.waitForFunction(() => Boolean(window.__state.releaseStop))
  await page.evaluate(() => {
    const state = window.__state
    state.oldRelease = state.releaseStop
    state.holdId = null
    state.tasks.find((item) => item.id === 11).run_token = 'new +/回合'
  })
  await page.locator('.card').filter({ has: page.getByRole('heading', { name: '任务列表' }) })
    .getByRole('button', { name: '刷新' }).click()
  await row('ingest-running').getByRole('button', { name: '停止生成' }).waitFor()
  await page.evaluate(() => window.__state.oldRelease())
  await waitForToast('任务已重新执行')
  assert.equal(await page.evaluate(() => window.__state.tasks.find((item) => item.id === 11).wiki_stop_requested), false)
  assert.equal(await page.evaluate(() => window.__toasts.some((toast) => toast.text === '已发送停止请求')), false)
  await row('ingest-running').getByRole('button', { name: '停止生成' }).click()
  await waitForToast('已发送停止请求')
  assert.deepEqual(await page.evaluate(() => window.__state.requests.filter((request) =>
    request.path === '/api/tasks/11/stop-wiki').map((request) => request.runToken)), ['run-11', 'new +/回合'])

  await mount('tasks')
  await row('ingest-running').getByRole('button', { name: '停止生成' }).waitFor()
  assert.equal(await row('readonly-wiki').getByRole('button', { name: '停止生成' }).count(), 0)
  assert.equal(await row('readonly-stopped').getByRole('button', { name: '重试' }).count(), 0)
  await page.evaluate(() => { window.__state.failId = 11 })
  await row('ingest-running').getByRole('button', { name: '停止生成' }).click()
  await waitForToast('模拟停止失败')
  await row('ingest-running').getByText('已停止', { exact: true }).waitFor()
  assert.equal(await page.evaluate(() => window.__toasts.some((toast) => toast.text === '已发送停止请求')), false)
  assert.equal(await requestCount('/api/tasks/11/stop-wiki'), 1)
  await page.getByRole('combobox', { name: '按任务状态筛选' }).selectOption('stopped')
  await row('stopped-wiki').waitFor()
  assert.equal(await row('readonly-wiki').count(), 0)
  assert.deepEqual(errors, [])
  console.log('PASS Wiki stop permissions, pending, queue, retry, refresh failure and API error handling')
} finally {
  await browser.close()
  server.close()
}

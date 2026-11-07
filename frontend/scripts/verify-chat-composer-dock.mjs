/**
 * 底部输入框固定验收：长会话下输入框是否仍停靠在视口底部。
 * 先单独启动 Vite（例如 pnpm exec vite --host 127.0.0.1 --port 15273），再运行：
 *   node scripts/verify-chat-composer-dock.mjs --label=before
 *   node scripts/verify-chat-composer-dock.mjs --label=after
 * 用 CHAT_DOCK_BASE_URL / CHAT_DOCK_OUT / CHAT_DOCK_VIEWPORT 覆盖地址、输出目录与视口。
 * 所有 /api 请求都在浏览器层拦截，脚本不会连真实后端；前后两次使用同一份
 * 模拟会话、同一视口（1440×900）与同一滚动位置，便于逐帧对比。
 */
import { chromium } from 'playwright'
import { mkdirSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const BASE = (process.env.CHAT_DOCK_BASE_URL || 'http://127.0.0.1:15273').replace(/\/+$/, '')
const LABEL = process.argv.find((a) => a.startsWith('--label='))?.slice('--label='.length) || 'after'
const VP = (process.env.CHAT_DOCK_VIEWPORT || '1440x900').split('x').map(Number)
const OUT = process.env.CHAT_DOCK_OUT || resolve(HERE, '..', '..', 'docs', 'screenshots', 'chat-input-dock')
const FILE = (name) => resolve(OUT, `${LABEL}-${name}.png`)

const now = '2026-11-07T08:10:00Z'
const LONG_ANSWER = [
  '根据知识库中《差旅与费用管理制度》与《2026 年企业级 AI Agent 市场报告》的记载，一线城市出差住宿上限为每晚 600 元，二线城市为每晚 400 元，其他城市为每晚 300 元。',
  '超出标准的部分需由部门负责人审批后方可报销，并需在报销单中注明原因。报告同时指出，平台层整合模型能力并支撑数字员工、知识库和工作流编排，',
  '其竞争要素包括落地确定性、可治理性和生态开放度；采购与厂商评估更关注可验证的投资回报、客户续约率和净收入留存率（NRR）等核心指标。',
  '未来发展方向预计将向多智能体协作、直接产出交付物和行业化发展。现有摘录未说明该报告的编制、审批或使用方面有哪些具体规定和要求。',
].join('')
const TURNS = [
  ['2026 年中国企业级 AI Agent 市场?', LONG_ANSWER],
  ['差旅住宿标准是怎么规定的？', LONG_ANSWER],
  ['我想知道知识库中是否有记录 2026 年中国企业级 AI Agent 市场', LONG_ANSWER],
  ['P0 工单的响应时限是多久？', LONG_ANSWER],
  ['报告里提到 NRR 指标了吗？', LONG_ANSWER],
  ['多智能体协作那一节还说了什么？', LONG_ANSWER],
]
const messages = TURNS.flatMap(([q, a], i) => ([
  { id: i * 2 + 1, role: 'user', content: q, kb_ids: [1], created_at: now, status: 'ok' },
  {
    id: i * 2 + 2, role: 'assistant', content: a, kb_ids: [1], created_at: now,
    status: 'ok', latency_ms: 71100, evidence: [], route: { kb_routes: [{ kb_id: 1, kb_name: '制度库', chunks: 10 }] },
    usage: { calls: 15, latency_ms: 71100, input_tokens: 39368, output_tokens: 862 },
    insufficient: false,
  },
]))
const conversation = {
  id: 2, title: '2026 年企业级 AI Agent 市场', kb_ids: [1], message_count: messages.length,
  pinned: false, created_at: now, updated_at: now, messages,
}
const user = {
  id: 1, username: 'admin', display_name: '系统管理员', role: 'admin',
  status: 'active', must_change_password: false, created_at: now, last_login_at: now,
}
const reply = async (route, json, status = 200) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(json) })

async function handle(route, unknown) {
  const req = route.request()
  const path = new URL(req.url()).pathname
  const method = req.method()
  if (path === '/api/meta') return reply(route, {
    version: '1.0.16', needs_setup: false, offline_models: false,
    capabilities: { agent_enabled: true, llm_configured: true, embedding_configured: true },
  })
  if (path === '/api/health') return reply(route, { ok: true, version: '1.0.16' })
  if (path === '/api/auth/me') return reply(route, { user, permissions: { is_admin: true, kb_grants: [] } })
  if (path === '/api/setup/org') return reply(route, { org_name: '企业知识库' })
  if (path === '/api/kbs') return reply(route, {
    items: [{ id: 1, name: '制度与报告库', mode: 'basic', mode_label: '基础', my_level: 'manage' }], total: 1,
  })
  if (path === '/api/conversations' && method === 'GET') return reply(route, {
    items: [{ ...conversation, messages: undefined }], total: 1,
  })
  if (path === '/api/conversations/2' && method === 'GET') return reply(route, conversation)
  if (path === '/api/conversations/2' && method === 'PATCH') return reply(route, { ...conversation, messages: undefined })
  unknown.push(`${method} ${path}`)
  return reply(route, {})
}

const measure = (page) => page.evaluate(() => {
  const rect = (sel) => {
    const el = document.querySelector(sel)
    if (!el) return null
    const r = el.getBoundingClientRect()
    return { top: Math.round(r.top), bottom: Math.round(r.bottom), height: Math.round(r.height) }
  }
  const scroll = document.querySelector('.chat-scroll')
  return {
    viewport: { w: innerWidth, h: innerHeight },
    document: {
      scrollHeight: document.documentElement.scrollHeight,
      clientHeight: document.documentElement.clientHeight,
      scrollTop: document.documentElement.scrollTop,
    },
    composer: rect('.chat-composer'),
    scrollArea: rect('.chat-scroll'),
    scrollMetrics: scroll ? { clientHeight: scroll.clientHeight, scrollHeight: scroll.scrollHeight, scrollTop: scroll.scrollTop } : null,
  }
})

const browser = await chromium.launch({ headless: true })
const context = await browser.newContext({ viewport: { width: VP[0], height: VP[1] }, locale: 'zh-CN' })
await context.addInitScript(() => localStorage.setItem('wrk_token', 'synthetic-test-token'))
const unknown = []
const pageErrors = []
await context.route('**/api/**', (route) => handle(route, unknown))
const page = await context.newPage()
page.on('pageerror', (e) => pageErrors.push(e.message))

mkdirSync(OUT, { recursive: true })
await page.goto(`${BASE}/#/chat/2`, { waitUntil: 'domcontentloaded' })
await page.locator('.chat-composer textarea').waitFor({ timeout: 15000 })
await page.getByRole('button', { name: /发送/ }).waitFor({ timeout: 15000 })
await page.waitForTimeout(1500)

// 统一场景：会话滚到顶部，页面本身不滚动；前后两次状态完全一致。
const setScroll = (top) => page.evaluate((value) => {
  window.scrollTo(0, 0)
  const el = document.querySelector('.chat-scroll')
  if (el) el.scrollTop = value === 'bottom' ? el.scrollHeight : 0
}, top)

const composerEnabled = await page.locator('.chat-composer textarea').isEnabled()

await setScroll('top')
await page.waitForTimeout(400)
const topMetrics = await measure(page)
await page.screenshot({ path: FILE('top'), animations: 'disabled' })

await setScroll('bottom')
await page.waitForTimeout(400)
const bottomMetrics = await measure(page)
await page.screenshot({ path: FILE('bottom'), animations: 'disabled' })

const docked = (m) => Boolean(
  m.composer && m.composer.top >= 0 && m.composer.bottom <= m.viewport.h + 1 &&
  m.scrollMetrics && m.scrollMetrics.scrollHeight > m.scrollMetrics.clientHeight &&
  m.document.scrollHeight <= m.document.clientHeight + 1,
)

const result = { label: LABEL, base: BASE, top: topMetrics, bottom: bottomMetrics, unknown, pageErrors }
result.composerDocked = { top: docked(topMetrics), bottom: docked(bottomMetrics) }
result.composerEnabled = composerEnabled
writeFileSync(resolve(OUT, `${LABEL}-metrics.json`), JSON.stringify(result, null, 2) + '\n')
console.log(JSON.stringify(result, null, 2))
await browser.close()

// before 只记录现状；after 作为回归门禁：长会话下输入框必须停靠在视口内。
if (pageErrors.length || unknown.length) process.exitCode = 1
if (LABEL.startsWith('after') && (!result.composerDocked.top || !result.composerDocked.bottom || !composerEnabled)) {
  console.error('FAIL: 长会话下底部输入框未固定在视口底部、消息区未自行滚动，或输入框不可用')
  process.exitCode = 1
}

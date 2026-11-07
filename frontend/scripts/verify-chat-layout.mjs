/**
 * 智能问答页整屏布局回归。
 *
 * 先单独启动 Vite（127.0.0.1:15173），再运行：
 *   node scripts/verify-chat-layout.mjs
 * 所有 /api 请求都被拦截，不会把测试消息发到真实后端。
 *
 * 覆盖两件事：长会话滚到底部时左侧会话列表必须留在视口内（消息区自己滚动），
 * 以及右侧会话区要有圆角描边。任意断言失败即非零退出。
 */
import { chromium } from 'playwright'

const BASE = (process.env.CHAT_TEST_BASE_URL || 'http://127.0.0.1:15173').replace(/\/+$/, '')
const now = '2026-11-07T08:02:00Z'
const user = { id: 1, username: 'admin', display_name: '系统管理员', role: 'admin' }
const permissions = { is_admin: true, kb_grants: [] }

function msg(id, role, content) {
  return {
    id, role, content, status: 'ok', kb_ids: [1], created_at: now,
    evidence: [], route: { kb_routes: [] }, latency_ms: 1580,
    usage: { calls: 3, latency_ms: 1580 }, insufficient: false,
  }
}

// 足够长，保证 900px 视口一定溢出，才能区分「整页滚动」和「消息区内部滚动」。
const answer = [
  '（离线演示模式：以下内容由检索片段抽取整理，未调用生成模型）',
  '• Q：知识库场景目前支持接入多少家模型供应商，故障转移成功率是多少？ [1]',
  '• Q：本合同的合同金额与服务期分别是多少？ [2]',
  '• Q：2026 年 8 月云启智能科技的单月营收和累计营收分别是多少？ [3]',
  '1. 私有化交付产能风险：若 6 名交付工程师不能按期到位，Q4 预计有 8–10 单存在延期交付风险，',
  '   影响约 2400 万元合同金额确认。报告建议优先补充交付团队并启用备用实施伙伴。',
  '2. 行业模板交付风险：三套行业模板若包容延期，将直接影响金融、医疗行业的 Q4 攻坚，',
  '   这两个行业合计目标金额约 3350 万元，占季度目标的 38%。',
  '3. 续约与 NRR 风险：当前续约率 92.4%，NRR 118% 处于健康区间，但 Q4 有 34 家客户',
  '   合同集中到期，其中 12 家尚未启动续约沟通，需要提前安排客户成功跟进。',
  '4. 合规清单：等保三级测评、数据出境评估与供应商安全审查均需在 Q4 完成。',
].join('\n')

const messages = []
for (let i = 0; i < 6; i += 1) {
  messages.push(msg(i * 2 + 1, 'user', `第 ${i + 1} 个问题：住宿标准是多少？`))
  messages.push(msg(i * 2 + 2, 'assistant', answer))
}

const conversations = [
  { id: 1, title: '出差住宿标准是多少？', pinned: true, message_count: 12, kb_ids: [1], created_at: now, updated_at: '2026-11-07T08:07:00Z' },
  { id: 2, title: '新会话 11/01 11:51', pinned: false, message_count: 6, kb_ids: [1], created_at: now, updated_at: '2026-11-07T07:02:00Z' },
  { id: 3, title: '合同审批流程', pinned: false, message_count: 2, kb_ids: [1], created_at: now, updated_at: '2026-11-06T13:38:00Z' },
]

async function reply(route, json, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(json) })
}
async function handle(route) {
  const req = route.request()
  const path = new URL(req.url()).pathname
  const method = req.method()
  if (path === '/api/meta') return reply(route, { version: '1.0.16', needs_setup: false, offline_models: false, capabilities: { agent_enabled: true } })
  if (path === '/api/auth/me') return reply(route, { user, permissions })
  if (path === '/api/auth/login' && method === 'POST') return reply(route, { token: 'layout-mock-token', user })
  if (path === '/api/auth/logout' && method === 'POST') return reply(route, { ok: true })
  if (path === '/api/setup/org') return reply(route, { org_name: '企业知识库' })
  if (path === '/api/kbs') return reply(route, { items: [{ id: 1, name: '制度与流程', mode: 'basic', mode_label: '基础', my_level: 'manage' }], total: 1 })
  if (path === '/api/conversations' && method === 'GET') return reply(route, { items: conversations, total: conversations.length })
  const match = path.match(/^\/api\/conversations\/(\d+)$/)
  if (match && method === 'GET') {
    const conv = conversations.find((c) => c.id === Number(match[1]))
    return reply(route, { ...conv, messages: conv.id === 1 ? messages : [], message_count: conv.id === 1 ? messages.length : 0 })
  }
  return reply(route, { items: [], total: 0 })
}

const browser = await chromium.launch({ headless: true })
const failures = []
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' })
  await context.route('**/api/**', (route) => handle(route))
  const page = await context.newPage()
  const pageErrors = []
  page.on('pageerror', (err) => pageErrors.push(err.message))

  await page.goto(`${BASE}/#/login`, { waitUntil: 'domcontentloaded' })
  await page.getByPlaceholder('请输入用户名').fill('admin')
  await page.getByPlaceholder('请输入密码').fill('demo-password')
  await page.locator('.login-card').getByRole('button', { name: '登录', exact: true }).click()
  await page.waitForFunction(() => !location.hash.startsWith('#/login'), null, { timeout: 15000 })

  await page.goto(`${BASE}/#/chat/1`, { waitUntil: 'domcontentloaded' })
  await page.locator('.chat-thread .msg').first().waitFor({ timeout: 15000 })
  await page.waitForTimeout(700)
  await page.evaluate(() => {
    const el = document.querySelector('.chat-scroll')
    if (el) el.scrollTop = el.scrollHeight
    window.scrollTo(0, document.documentElement.scrollHeight)
  })
  await page.waitForTimeout(300)

  const m = await page.evaluate(() => {
    const scroll = document.querySelector('.chat-scroll')
    const main = document.querySelector('.chat-main')
    const conv = document.querySelector('.chat-conv-item')
    const style = main ? getComputedStyle(main) : null
    return {
      innerHeight: window.innerHeight,
      docScrollHeight: document.documentElement.scrollHeight,
      windowScrollY: window.scrollY,
      chatClientHeight: scroll?.clientHeight ?? 0,
      chatScrollHeight: scroll?.scrollHeight ?? 0,
      chatScrollTop: scroll?.scrollTop ?? 0,
      firstConvTop: conv?.getBoundingClientRect().top ?? -1,
      radius: style ? parseFloat(style.borderTopLeftRadius) : 0,
      borderWidth: style ? parseFloat(style.borderTopWidth) : 0,
    }
  })

  const check = (label, ok, detail) => {
    console.log(`${ok ? '[PASS]' : '[FAIL]'} ${label}${detail ? ` — ${detail}` : ''}`)
    if (!ok) failures.push(label)
  }
  check('长会话不撑高整页', m.docScrollHeight <= m.innerHeight + 1, `doc=${m.docScrollHeight}, viewport=${m.innerHeight}`)
  check('页面本身不滚动', m.windowScrollY === 0, `scrollY=${m.windowScrollY}`)
  check('消息区内部滚动', m.chatScrollHeight > m.chatClientHeight + 100 && m.chatScrollTop > 0,
    `client=${m.chatClientHeight}, scroll=${m.chatScrollHeight}, top=${m.chatScrollTop}`)
  check('左侧会话列表留在视口内', m.firstConvTop >= 0 && m.firstConvTop < m.innerHeight, `convTop=${m.firstConvTop}`)
  check('右侧会话区有圆角描边', m.radius >= 8 && m.borderWidth >= 1, `radius=${m.radius}, border=${m.borderWidth}`)
  check('无浏览器运行时错误', pageErrors.length === 0, pageErrors.join('; '))
  await context.close()
} finally {
  await browser.close()
}

if (failures.length) {
  console.error(`\n${failures.length} 项布局回归失败`)
  process.exitCode = 1
} else {
  console.log('\n问答页布局检查全部通过')
}

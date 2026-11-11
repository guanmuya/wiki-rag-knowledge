// 拍摄「当前版本界面图」：输出到 docs/screenshots/current/。
//
// 这是**人工验收辅助脚本**，不是自动化测试：不接入 CI、不进 pnpm test。
// 跑之前需要三样东西：
//   1) 后端在 http://127.0.0.1:8000 跑着（演示数据已灌：backend/scripts/seed_demo.py）
//   2) Vite 在 http://127.0.0.1:5173 跑着（frontend 下 node node_modules/vite/bin/vite.js --port 5173 --host 127.0.0.1）
//   3) 前端依赖已装（playwright 在 frontend/node_modules 里）
//
// 用法（在 frontend/ 下执行，这样能解析到 playwright）：
//   UI_PASS=<管理员口令> node ../scripts/dev-only/capture-ui.mjs
// 可选环境变量：UI_BASE（默认 http://127.0.0.1:5173）、UI_USER（默认 admin）
//
// 注意：本项目用 HashRouter（Electron 用 file:// 加载），所以路由形如 /#/kbs 而不是 /kbs。
import { createRequire } from 'node:module'
import { mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..', '..')
// playwright 装在 frontend/node_modules 里；本脚本在 scripts/dev-only/，直接用裸模块名解析不到，
// 因此按 frontend/package.json 的位置做 require 解析（仍然是最小依赖，不额外引第三方包）。
const require = createRequire(resolve(ROOT, 'frontend', 'package.json'))
const { chromium } = require('playwright')
const OUT = resolve(ROOT, 'docs', 'screenshots', 'current')
mkdirSync(OUT, { recursive: true })

const BASE = process.env.UI_BASE || 'http://127.0.0.1:5173'
const USER = process.env.UI_USER || 'admin'
const PASS = process.env.UI_PASS || ''

if (!PASS) {
  console.error('缺少 UI_PASS：请传管理员口令，例如 UI_PASS=<口令> node ../scripts/dev-only/capture-ui.mjs')
  process.exit(2)
}

const problems = []
const shots = []

function watch(page, tag) {
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return
    const text = msg.text()
    if (text.includes('Download the React DevTools')) return
    problems.push(`[${tag}] console: ${text.slice(0, 200)}`)
  })
  page.on('pageerror', (err) => problems.push(`[${tag}] pageerror: ${String(err).slice(0, 200)}`))
}

async function shot(page, name, wait = 900) {
  await page.waitForTimeout(wait)
  await page.screenshot({ path: resolve(OUT, `${name}.png`), fullPage: false })
  shots.push(name)
  console.log(`  shot ${name}.png`)
}

const run = async () => {
  const browser = await chromium.launch()
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' })
  const page = await ctx.newPage()
  watch(page, 'main')

  console.log('1) 登录页')
  await page.goto(`${BASE}/#/login`, { waitUntil: 'domcontentloaded' })
  await shot(page, '01-login')

  await page.fill('input[autocomplete="username"]', USER)
  await page.fill('input[type="password"]', PASS)
  await page.click('button[type="submit"]')
  await page.waitForURL((u) => u.hash.includes('/kbs'), { timeout: 20000 })
  await page.waitForTimeout(1500)

  console.log('2) 知识库列表 / 详情三个页签')
  await page.goto(`${BASE}/#/kbs`, { waitUntil: 'domcontentloaded' })
  await shot(page, '02-kb-list')
  await page.goto(`${BASE}/#/kbs/1`, { waitUntil: 'domcontentloaded' })
  await shot(page, '03-kb-documents')
  await page.goto(`${BASE}/#/kbs/1?tab=wiki`, { waitUntil: 'domcontentloaded' })
  await shot(page, '04-kb-wiki', 2000)
  // 换到最后一页并往下滚一屏：这里要展示的是「正文 + 来源依据（证据回挂）」，与 04 不重复
  const lastPage = page.getByText(/七、离职管理/).first()
  if (await lastPage.count()) {
    await lastPage.click()
    await page.waitForTimeout(1500)
    await page.mouse.move(1000, 600)
    await page.mouse.wheel(0, 700)
    await shot(page, '05-kb-wiki-page', 800)
  }
  await page.goto(`${BASE}/#/kbs/1?tab=settings`, { waitUntil: 'domcontentloaded' })
  await shot(page, '06-kb-settings', 2000)

  console.log('3) 智能问答')
  await page.goto(`${BASE}/#/chat`, { waitUntil: 'domcontentloaded' })
  await shot(page, '07-chat-empty', 1800)
  const boxes = page.locator('input[type="checkbox"]')
  const n = await boxes.count()
  for (let i = 0; i < Math.min(n, 2); i++) await boxes.nth(i).check({ force: true }).catch(() => {})
  const input = page.locator('textarea').first()
  if (await input.count()) {
    await input.fill('一线城市出差住宿上限是多少？P0 工单多久响应？')
    const send = page.getByRole('button', { name: /发送|提问|提交/ }).first()
    if (await send.count()) await send.click()
    else await input.press('Enter')
    await shot(page, '08-chat-answer', 12000)
  } else {
    problems.push('问答页找不到输入框')
  }

  console.log('4) 检索调试（先跑一次检索，否则页面是空态）')
  await page.goto(`${BASE}/#/search`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1800)
  const sboxes = page.locator('input[type="checkbox"]')
  const sn = await sboxes.count()
  for (let i = 0; i < sn; i++) await sboxes.nth(i).check({ force: true }).catch(() => {})
  const q = page.locator('input[placeholder*="试用期"]').first()
  if (await q.count()) {
    await q.fill('试用期多久？转正流程是什么？')
    const btn = page.getByRole('button', { name: /运行检索/ }).first()
    if (await btn.count()) await btn.click()
  }
  await shot(page, '09-search', 6000)

  console.log('5) 任务 / 用户 / 模型 / 用量 / 关于 / 个人 Agent')
  const pages = [
    ['/tasks', '10-tasks'],
    ['/admin/users', '11-users'],
    ['/admin/models', '12-models'],
    ['/admin/usage', '13-usage'],
    ['/about', '14-about'],
    ['/agent', '15-agent'],
  ]
  for (const [path, name] of pages) {
    await page.goto(`${BASE}/#${path}`, { waitUntil: 'domcontentloaded' })
    await shot(page, name, 2000)
    const text = await page.locator('body').innerText()
    if (text.length < 60) problems.push(`${path}: 页面内容为空`)
  }

  await browser.close()

  console.log(`\n共 ${shots.length} 张 → docs/screenshots/current/`)
  if (problems.length) {
    console.log(`发现 ${problems.length} 个问题：`)
    problems.forEach((p) => console.log('  -', p))
    process.exitCode = 1
  } else {
    console.log('未发现前端运行时错误')
  }
}

run().catch((e) => {
  console.error('截图脚本异常：', e)
  process.exitCode = 2
})

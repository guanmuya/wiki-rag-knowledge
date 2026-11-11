/**
 * 管理员用量页的端到端回归测试。
 *
 * 为什么单独一个文件：`tests/e2e.test.mjs` 的 5 条用例是发布口径里的「E2E 5/5」，
 * 往里塞新用例会让这个数字失去意义（见 tests/README.md §4）。这里同样用
 * 真实 Chromium + 真实 Vite + 内存 mock 后端，只是换个端口和缓存目录，
 * 因为 `node --test` 会并行跑多个测试文件。
 *
 * 覆盖的是这次重做的四件事：
 *  1. 六张指标卡与页脚口径（金额是「估算」、未收录的维度逐项点名）；
 *  2. 趋势图切指标、切柱/折线（柱 → 折线要真的换图，不只是换标题）；
 *  3. 未计价 / 没有 Token 记录的提示不会被 0 掩盖；
 *  4. 单价弹窗保存覆盖价真的发出 PUT，且保存后重新拉用量。
 */
import { after, before, describe, test } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'
import { chromium } from 'playwright'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
// 与 tests/e2e.test.mjs 的 15173 分开：两个测试文件并行启动各自的 Vite。
const PORT = Number(process.env.USAGE_E2E_PORT || 15174)
const BASE = `http://127.0.0.1:${PORT}`
const NOW = '2026-01-31T00:00:00Z'
// 只进 mock 后端的假令牌：不需要任何真实凭据，也不碰其它测试文件的常量。
const TOKEN = 'usage-page-e2e-token'

let viteServer
let browser

const clone = (value) => JSON.parse(JSON.stringify(value))

/** 轮询等待：断言 UI 是异步渲染的，不能用一次快照判生死。 */
async function waitFor(check, label, timeout = 20000) {
  const deadline = Date.now() + timeout
  let lastError
  while (Date.now() < deadline) {
    try {
      const value = await check()
      if (value) return value
    } catch (err) {
      lastError = err
    }
    await new Promise((resolvePause) => setTimeout(resolvePause, 50))
  }
  throw new Error(`等待超时：${label}${lastError ? `（${lastError.message}）` : ''}`)
}

/** 一个汇总桶：字段与 `UsageTotals` 对齐，测试里只覆盖关心的那几个。 */
function bucket(overrides = {}) {
  return {
    calls: 0,
    live_calls: 0,
    offline_calls: 0,
    chat_calls: 0,
    embed_calls: 0,
    vision_calls: 0,
    prompt_tokens: 0,
    completion_tokens: 0,
    embed_tokens: 0,
    total_tokens: 0,
    latency_ms: 0,
    record_count: 0,
    unmetered_records: 0,
    cost: { currency: 'USD', amount: 0, priced_records: 0, unpriced_records: 0, unpriced_tokens: 0 },
    ...overrides,
  }
}

const TOTALS = {
  calls: 120,
  live_calls: 90,
  offline_calls: 20,
  chat_calls: 60,
  embed_calls: 30,
  vision_calls: 4,
  prompt_tokens: 120000,
  completion_tokens: 45000,
  embed_tokens: 30000,
  total_tokens: 195000,
  latency_ms: 0,
  record_count: 110,
  unmetered_records: 3,
  cost: {
    currency: 'USD',
    amount: 2.5,
    priced_records: 96,
    unpriced_records: 14,
    unpriced_tokens: 30000,
  },
}

/** 30 个有记录的日期：柱状图 30 × 3 段，写死数字，重复运行结果一致。 */
const DAILY = Array.from({ length: 30 }, (_, index) => bucket({
  date: `2026-01-${String(index + 1).padStart(2, '0')}`,
  calls: 4,
  live_calls: 3,
  offline_calls: 1,
  chat_calls: 2,
  embed_calls: 1,
  prompt_tokens: 4000,
  completion_tokens: 1500,
  embed_tokens: 1000,
  total_tokens: 6500,
  record_count: 4,
  cost: { currency: 'USD', amount: 0.08, priced_records: 3, unpriced_records: 1, unpriced_tokens: 1000 },
}))

function makeUsage(days) {
  return {
    period_days: days,
    timezone: 'UTC',
    currency: 'USD',
    price_unit: 'per_1m_tokens',
    totals: bucket(clone(TOTALS)),
    daily: days === 0 ? clone(DAILY) : clone(DAILY.slice(-days)),
    by_source: [
      bucket({
        source: 'processing', label: '知识库处理', calls: 40,
        prompt_tokens: 60000, completion_tokens: 20000, total_tokens: 80000,
        cost: { currency: 'USD', amount: 1.4, priced_records: 38, unpriced_records: 2, unpriced_tokens: 4000 },
      }),
      bucket({
        source: 'chat', label: '问答', calls: 70,
        prompt_tokens: 50000, completion_tokens: 20000, total_tokens: 70000,
        cost: { currency: 'USD', amount: 0.9, priced_records: 55, unpriced_records: 10, unpriced_tokens: 20000 },
      }),
      bucket({
        source: 'agent', label: '个人 Agent', calls: 10, unmetered_records: 3,
        prompt_tokens: 10000, completion_tokens: 5000, total_tokens: 15000,
        cost: { currency: 'USD', amount: 0.2, priced_records: 3, unpriced_records: 2, unpriced_tokens: 6000 },
      }),
    ],
    by_core: [
      bucket({
        core_id: 'codex', label: 'Codex CLI', calls: 10, unmetered_records: 3,
        prompt_tokens: 10000, completion_tokens: 5000, total_tokens: 15000,
        cost: { currency: 'USD', amount: 0.2, priced_records: 3, unpriced_records: 2, unpriced_tokens: 6000 },
      }),
    ],
  }
}

function makeCatalog() {
  const builtin = {
    model_id: 'gpt-6-astra',
    currency: 'USD',
    price_unit: 'per_1m_tokens',
    input_per_1m: 10,
    output_per_1m: 50,
    source: 'OpenAI 官方价目表',
    source_url: 'https://example.test/pricing',
    verified_at: '2026-01-01',
    note: '',
  }
  return {
    currency: 'USD',
    price_unit: 'per_1m_tokens',
    reference: true,
    note: '参考价：取自公开价目页，不用于账单；管理员可以为具体模型配置填自己的价格。',
    sources: [{ id: 'openai-pricing', name: 'OpenAI 官方价目表', url: 'https://example.test/pricing' }],
    items: [
      {
        model_config_id: 1,
        model: 'gpt-6-astra',
        label: '主对话模型',
        provider: 'OpenAI',
        purpose: 'llm',
        match_key: 'gpt-6-astra',
        builtin: { ...builtin },
        user: null,
        effective: { origin: 'builtin', ...builtin },
      },
      {
        model_config_id: 2,
        model: 'text-embed-3',
        label: '向量模型',
        provider: 'OpenAI',
        purpose: 'embedding',
        match_key: 'text-embed-3',
        builtin: null,
        user: null,
        effective: {
          origin: 'none',
          currency: 'USD',
          price_unit: 'per_1m_tokens',
          input_per_1m: null,
          output_per_1m: null,
          model_id: 'text-embed-3',
          source: '',
          source_url: '',
          verified_at: '',
          note: '',
        },
      },
    ],
  }
}

/**
 * 确定性的内存后端。未知接口返回 200 {} 并记进 `state.unknown`：
 * 写完断言先看它，别让页面悄悄打到没 mock 的接口。
 */
function createMock() {
  const user = {
    id: 1,
    username: 'admin',
    display_name: '系统管理员',
    role: 'admin',
    is_active: true,
    must_change_password: false,
    created_at: NOW,
  }
  const state = {
    usageRequests: [],
    pricingReads: 0,
    priceWrites: [],
    priceClears: [],
    unknown: [],
    catalog: makeCatalog(),
  }

  const json = (route, body, status = 200) => route.fulfill({ status, json: body })

  async function handle(route) {
    const req = route.request()
    const url = new URL(req.url())
    const path = url.pathname
    const method = req.method()

    if (path === '/api/meta') {
      return await json(route, {
        app_name: '企业知识库',
        initialized: true,
        version: '1.0.18-usage',
        offline_models: true,
        needs_setup: false,
      })
    }
    if (path === '/api/health') {
      return await json(route, { ok: true, version: '1.0.18-usage' })
    }
    if (path === '/api/setup/status') {
      return await json(route, {
        initialized: true,
        org_name: '测试组织',
        app_name: '企业知识库',
        version: '1.0.18-usage',
      })
    }
    if (path === '/api/setup/org') return await json(route, { org_name: '测试组织' })
    if (path === '/api/auth/me') {
      return await json(route, { user, permissions: { is_admin: true, kb_grants: [] } })
    }

    if (path === '/api/usage') {
      state.usageRequests.push(url.search)
      const days = Number(url.searchParams.get('days') ?? 30)
      return await json(route, makeUsage(days))
    }

    if (path === '/api/settings/pricing' && method === 'GET') {
      state.pricingReads += 1
      return await json(route, clone(state.catalog))
    }

    const priceMatch = path.match(/^\/api\/settings\/pricing\/(\d+)$/)
    if (priceMatch && (method === 'PUT' || method === 'DELETE')) {
      const id = Number(priceMatch[1])
      const item = state.catalog.items.find((row) => row.model_config_id === id)
      if (!item) return await json(route, { detail: '模型配置不存在' }, 404)
      if (method === 'PUT') {
        const body = req.postDataJSON()
        state.priceWrites.push({
          id,
          body: clone(body),
          authorization: req.headers().authorization ?? '',
        })
        item.user = {
          currency: 'USD',
          price_unit: 'per_1m_tokens',
          input_per_1m: body.input_per_1m,
          output_per_1m: body.output_per_1m,
          updated_by: 1,
          updated_at: NOW,
        }
        item.effective = {
          ...item.effective,
          origin: 'user',
          input_per_1m: body.input_per_1m,
          output_per_1m: body.output_per_1m,
          source: '管理员填写',
          source_url: '',
          verified_at: '',
        }
        return await json(route, clone(item))
      }
      state.priceClears.push(id)
      item.user = null
      item.effective = item.builtin
        ? { origin: 'builtin', ...clone(item.builtin) }
        : {
            origin: 'none',
            currency: 'USD',
            price_unit: 'per_1m_tokens',
            input_per_1m: null,
            output_per_1m: null,
            model_id: item.model,
            source: '',
            source_url: '',
            verified_at: '',
            note: '',
          }
      return await json(route, clone(item))
    }

    state.unknown.push(`${method} ${path}`)
    return await json(route, {})
  }

  return { handle, state }
}

/** 每个用例一个独立浏览器上下文：localStorage 与路由状态互不污染。 */
async function openWithMock() {
  const mock = createMock()
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    locale: 'zh-CN',
  })
  await context.addInitScript((token) => {
    localStorage.setItem('wrk_token', token)
  }, TOKEN)
  await context.route('**/api/**', (route) => mock.handle(route))
  const page = await context.newPage()
  const pageErrors = []
  page.on('pageerror', (err) => pageErrors.push(err.message))
  return { context, page, mock, pageErrors }
}

async function openUsagePage(page) {
  await page.goto(`${BASE}/#/admin/usage`, { waitUntil: 'domcontentloaded' })
  await waitFor(
    () => page.locator('.usage-summary').isVisible().catch(() => false),
    '用量指标卡渲染',
    30000,
  )
}

describe('管理员用量页（离线、可重复）', () => {
  before(async () => {
    viteServer = await createServer({
      root: ROOT,
      configFile: resolve(ROOT, 'vite.config.ts'),
      base: '/',
      logLevel: 'error',
      // 独立缓存目录：`node --test` 会并行跑 tests/e2e.test.mjs，
      // 两个 Vite 抢同一个 node_modules/.vite 会互相覆盖依赖预构建结果。
      cacheDir: resolve(ROOT, 'node_modules/.vite-usage-page'),
      server: { host: '127.0.0.1', port: PORT, strictPort: true },
    })
    await viteServer.listen()
    browser = await chromium.launch({ headless: true })
  })

  after(async () => {
    await browser?.close()
    await viteServer?.close()
  })

  test('六张指标卡按接口数据渲染，金额标明是估算，页脚点名未收录的维度', { timeout: 60000 }, async () => {
    const { context, page, mock, pageErrors } = await openWithMock()
    try {
      await openUsagePage(page)
      assert.equal(await page.locator('.usage-metric').count(), 6)

      const labels = await page.locator('.usage-metric-label').allInnerTexts()
      for (const expected of [
        '输入 Token',
        '输出 Token',
        '向量 Token',
        '调用与操作',
        '估算费用',
        '未计价 Token',
      ]) {
        assert.ok(
          labels.some((text) => text.includes(expected)),
          `少了指标卡「${expected}」，实际是：${labels.join(' / ')}`,
        )
      }

      const summary = await page.locator('.usage-summary').innerText()
      assert.ok(summary.includes('120,000'), `输入 Token 应按 mock 显示 120,000：${summary}`)
      assert.ok(summary.includes('45,000'), '输出 Token 应按 mock 显示 45,000')
      // 「30,000」在未计价 Token 卡里也有，所以要限定在第三张卡上查向量 Token。
      const embedCard = await page.locator('.usage-metric').nth(2).innerText()
      assert.ok(embedCard.includes('30,000'), `向量 Token 应按 mock 显示 30,000：${embedCard}`)
      assert.match(summary, /在线 90 · 离线 20 · 未区分 10/)

      const costCard = page.locator('.usage-metric').nth(4)
      const costText = await costCard.innerText()
      assert.ok(costText.includes('$2.50'), `估算费用要按美元显示：${costText}`)
      assert.match(costText, /已计价 96 次 · 未计价 14 次/)
      // 徽标要单独查元素：卡片的标题「估算费用」本身就含「估算」二字。
      const badges = await costCard.locator('.badge').allInnerTexts()
      assert.ok(badges.includes('估算'), `费用卡要有「估算」徽标，实际是：${badges.join(' / ')}`)
      assert.ok(badges.includes('含未计价'), `有未计价记录时要有「含未计价」徽标，实际是：${badges.join(' / ')}`)

      const footer = await page.locator('.usage-footer').innerText()
      for (const word of ['缓存', '首字', '速度', '失败', '供应商', '明细']) {
        assert.ok(footer.includes(word), `页脚应逐项点名未收录的维度：缺少「${word}」`)
      }

      const breakdown = await page.locator('.usage-breakdown').innerText()
      assert.ok(breakdown.includes('知识库处理'), '来源表应列出知识库处理')
      assert.ok(breakdown.includes('$1.40'), '来源表要有估算费用列，并且按美元显示')
      assert.ok(
        breakdown.includes('个人 Agent 按核心') && breakdown.includes('Codex CLI'),
        'by_core 不为空时要显示「个人 Agent 按核心」表',
      )

      assert.ok(mock.state.usageRequests.length > 0, '用量接口确实被请求过')
      assert.ok(
        mock.state.usageRequests.every((search) => search.includes('days=30')),
        `默认应按近 30 天请求：${mock.state.usageRequests.join(' , ')}`,
      )

      // 时间范围只有 7/30/全部三档（后端只接受这三个值），切换必须真的改查询参数。
      const periods = page.getByRole('group', { name: '选择统计时间范围' })
      await periods.getByRole('button', { name: '近 7 天' }).click()
      await waitFor(
        () => mock.state.usageRequests.some((search) => search.includes('days=7')),
        '切到近 7 天后按 days=7 重新请求',
      )
      await periods.getByRole('button', { name: '全部' }).click()
      await waitFor(
        () => mock.state.usageRequests.some((search) => search.includes('days=0')),
        '切到全部后按 days=0 重新请求',
      )

      assert.deepEqual(mock.state.unknown, [], `有接口没被 mock：${mock.state.unknown.join(' , ')}`)
      assert.deepEqual(pageErrors, [], `页面不应有未捕获异常：${pageErrors.join('; ')}`)
    } finally {
      await context.close()
    }
  })

  test('切换趋势指标与柱/折线样式：图例、柱子与折线都真的换掉', { timeout: 60000 }, async () => {
    const { context, page } = await openWithMock()
    try {
      await openUsagePage(page)
      const chart = page.locator('.usage-chart')
      await chart.waitFor({ timeout: 20000 })

      assert.match(await chart.getAttribute('aria-label'), /Token趋势图（柱状）/)
      assert.equal(
        await chart.locator('rect').count(),
        90,
        'Token 指标应是 30 天 × 3 段堆叠柱',
      )

      // 段数对不代表堆叠对：三段必须首尾相接，而不是三根柱子画在同一高度。
      const stack = await chart.locator('.usage-chart-point').first().locator('rect').evaluateAll(
        (rects) => rects.map((rect) => ({
          y: Number(rect.getAttribute('y')),
          height: Number(rect.getAttribute('height')),
        })),
      )
      assert.equal(stack.length, 3, '每天应有 3 段柱')
      assert.ok(stack.every((bar) => bar.height > 0), `每段都该有高度：${JSON.stringify(stack)}`)
      for (let i = 1; i < stack.length; i += 1) {
        assert.ok(
          Math.abs(stack[i].y + stack[i].height - stack[i - 1].y) < 0.01,
          `第 ${i + 1} 段应正好接在第 ${i} 段上方：${JSON.stringify(stack)}`,
        )
      }

      await page.getByRole('group', { name: '趋势指标' }).getByRole('button', { name: '费用' }).click()
      await waitFor(
        async () => (await chart.locator('rect').count()) === 30,
        '切成费用后变成 30 根单序列柱',
      )
      assert.match(await chart.getAttribute('aria-label'), /费用趋势图（柱状）/)
      const costLegend = await page.locator('.usage-legend').innerText()
      assert.ok(costLegend.includes('估算费用'), `图例应说明当前画的是费用：${costLegend}`)

      await page.getByRole('group', { name: '趋势样式' }).getByRole('button', { name: '折线' }).click()
      await waitFor(
        async () => (await chart.locator('polyline').count()) === 1,
        '折线模式下出现 1 条折线',
      )
      assert.equal(await chart.locator('rect').count(), 0, '折线模式不应还留着柱子')
      assert.match(await chart.getAttribute('aria-label'), /费用趋势图（折线）/)
    } finally {
      await context.close()
    }
  })

  test('未计价与「无 Token 记录」不会被 0 Token 掩盖', { timeout: 60000 }, async () => {
    const { context, page } = await openWithMock()
    try {
      await openUsagePage(page)
      const alert = page.locator('.usage-metrics-alert')
      await alert.waitFor({ timeout: 20000 })
      assert.match(
        await alert.innerText(),
        /3 次调用没有留下 Token 记录/,
        '没有 Token 记录的调用要在指标卡下方显式提示',
      )

      const summary = await page.locator('.usage-summary').innerText()
      assert.ok(summary.includes('含未计价'), '有未计价记录时费用卡要有「含未计价」警告徽标')
      assert.match(summary, /无 Token 记录 3 次/, '未计价 Token 卡要说明还有多少次调用没留下 Token')
    } finally {
      await context.close()
    }
  })

  test('单价面板保存覆盖价：发出 PUT、行变成「自己填的价」、用量重新拉取', { timeout: 90000 }, async () => {
    const { context, page, mock } = await openWithMock()
    try {
      await openUsagePage(page)
      const usageBefore = mock.state.usageRequests.length

      await page.getByRole('button', { name: '管理单价' }).click()
      const modal = page.getByRole('dialog', { name: '模型单价' })
      await modal.waitFor({ timeout: 20000 })

      const llmRow = modal.locator('.pricing-row').filter({ hasText: '主对话模型' })
      await waitFor(() => llmRow.count().then((count) => count === 1), '单价面板加载出模型行')
      assert.match(await llmRow.innerText(), /内置参考价/, '没填过价时应显示内置参考价')

      const embedRow = modal.locator('.pricing-row').filter({ hasText: '向量模型' })
      assert.match(
        await embedRow.innerText(),
        /向量 Token 不参与估算/,
        '向量模型要说明它的价格不参与金额',
      )

      await llmRow.getByLabel('主对话模型 输入单价').fill('3.5')
      await llmRow.getByLabel('主对话模型 输出单价').fill('12')
      await llmRow.getByRole('button', { name: '保存' }).click()

      await waitFor(() => mock.state.priceWrites.length === 1, '保存请求已发出')
      assert.equal(mock.state.priceWrites[0].id, 1)
      assert.deepEqual(mock.state.priceWrites[0].body, { input_per_1m: 3.5, output_per_1m: 12 })
      assert.equal(
        mock.state.priceWrites[0].authorization,
        `Bearer ${TOKEN}`,
        '保存要带登录令牌',
      )

      await waitFor(
        async () => (await llmRow.innerText()).includes('自己填的价'),
        '保存后该行徽标变成「自己填的价」',
      )

      // 「恢复参考价」之后输入框必须清空：否则内置价的数字看起来像一条待保存的修改，
      // 一次误点就会把内置价钉成覆盖价、从此不再跟随参考目录。
      await llmRow.getByRole('button', { name: '恢复参考价' }).click()
      await waitFor(() => mock.state.priceClears.length === 1, '恢复参考价请求已发出')
      await waitFor(
        async () => (await llmRow.innerText()).includes('内置参考价'),
        '删除覆盖价后徽标回到「内置参考价」',
      )
      assert.equal(await llmRow.getByLabel('主对话模型 输入单价').inputValue(), '')
      assert.equal(await llmRow.getByLabel('主对话模型 输出单价').inputValue(), '')
      assert.equal(
        await llmRow.getByRole('button', { name: '保存' }).isDisabled(),
        true,
        '清空后「保存」应当是灰的，等管理员重新填价',
      )

      await modal.locator('.modal-foot').getByRole('button', { name: '关闭' }).click()
      await modal.waitFor({ state: 'detached', timeout: 20000 })
      assert.ok(
        mock.state.usageRequests.length > usageBefore,
        '保存单价后要重新拉一次用量，金额卡才会跟着变',
      )
    } finally {
      await context.close()
    }
  })
})

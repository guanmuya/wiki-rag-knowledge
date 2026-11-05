// Start the frontend Vite server, then run: node frontend/scripts/verify-setup-flow.cjs
// Set UI_BASE to override the default test server. All setup/meta requests are
// intercepted; the script never creates an administrator in the backend.
const { chromium } = require('playwright')
const assert = require('node:assert/strict')

const base = process.env.UI_BASE || 'http://127.0.0.1:5173'
const setupUrl = `${base}/#/setup`
const meta = (initialized) => ({
  needs_setup: !initialized,
  app_name: '企业知识库',
  version: '1.0.12',
  offline_models: true,
})
const status = (initialized) => ({
  initialized,
  org_name: '',
  app_name: '企业知识库',
  version: '1.0.12',
})

async function checkValidation(browser) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' })
  let posts = 0
  await page.route('**/api/meta', route => route.fulfill({ json: meta(false) }))
  await page.route('**/api/setup/status', route => route.fulfill({ json: status(false) }))
  await page.route('**/api/setup/admin', async route => {
    posts += 1
    await route.fulfill({ status: 400, json: { detail: '服务端校验占位' } })
  })
  try {
    await page.goto(setupUrl, { waitUntil: 'domcontentloaded' })
    await page.getByRole('heading', { name: '初始化系统' }).waitFor()
    const inputs = page.locator('.login-card input')
    const submit = page.getByRole('button', { name: '创建管理员并进入系统' })
    const cases = [
      ['ASCII 两类', 'abcde12345', false, '密码需包含'],
      ['ASCII 三类', 'Abcdef1234', true, '服务端校验占位'],
      ['中文非符号', 'abcd中文中文中文', true, '服务端校验占位'],
      ['上标数字', 'abcdefG中文²²', true, '服务端校验占位'],
      ['阿拉伯数字', 'abcdefghG١', true, '服务端校验占位'],
      ['表情计字符数', 'Abcdef1😀', false, '密码至少 10 位'],
      ['表情有效长度', 'abcdeFGhi😀', true, '服务端校验占位'],
    ]
    for (const [name, value, submits, message] of cases) {
      await inputs.nth(3).fill(value)
      await inputs.nth(4).fill(value)
      if (name === '中文非符号') {
        assert.equal(await page.locator('.pwd-meter-fill').getAttribute('style'), 'width: 40%;')
        assert.match(await page.locator('.pwd-meter-text').innerText(), /参考/)
      }
      const before = posts
      await submit.click()
      await page.waitForFunction(msg => document.querySelector('.login-error')?.textContent?.includes(msg), message)
      assert.equal(posts - before, submits ? 1 : 0, name)
      console.log(`PASS ${name}: ${submits ? 'submitted for server validation' : 'blocked locally'}`)
    }
    const hints = await page.locator('.field-hint').allTextContents()
    assert.match(hints.join(' '), /大写、小写、数字、符号中的至少三类/)
  } finally {
    await page.close()
  }
}

async function checkNavigation(browser, mode) {
  const page = await browser.newPage({ locale: 'zh-CN' })
  let initialized = false
  let afterCreateChecks = 0
  let metaCount = 0
  let modeNow = mode
  await page.route('**/api/setup/status', async route => {
    if (initialized) afterCreateChecks += 1
    await route.fulfill({ json: status(initialized) })
  })
  await page.route('**/api/setup/admin', async route => {
    initialized = true
    await route.fulfill({ status: 201, json: { ok: true, user_id: 1, username: 'admin' } })
  })
  await page.route('**/api/meta', async route => {
    metaCount += 1
    if (initialized && modeNow === 'delayed') await new Promise(resolve => setTimeout(resolve, 1500))
    if (initialized && modeNow === 'failure') return route.fulfill({ status: 503, json: { detail: 'simulated failure' } })
    return route.fulfill({ json: meta(initialized) })
  })
  try {
    await page.goto(setupUrl, { waitUntil: 'domcontentloaded' })
    await page.getByRole('heading', { name: '初始化系统' }).waitFor()
    const inputs = page.locator('.login-card input')
    await inputs.nth(3).fill('Abcdef1234')
    await inputs.nth(4).fill('Abcdef1234')
    const start = Date.now()
    await page.getByRole('button', { name: '创建管理员并进入系统' }).click()
    await page.getByRole('heading', { name: '登录', exact: true }).waitFor({ timeout: 1200 })
    const elapsed = Date.now() - start
    await page.waitForTimeout(1750)
    assert.equal(new URL(page.url()).hash, '#/login')
    assert.equal(afterCreateChecks, 0, 'setup should not remount after success')
    console.log(`PASS ${mode}: login after ${elapsed} ms; no extra setup status requests`)

    modeNow = 'normal'
    const beforeReload = metaCount
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.getByRole('heading', { name: '登录', exact: true }).waitFor()
    assert.ok(metaCount > beforeReload, 'initialized reload must recheck meta')
    console.log(`PASS ${mode}: initialized reload rechecked meta and stayed at login`)

    initialized = false
    const beforeUninitializedReload = metaCount
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.getByRole('heading', { name: '初始化系统' }).waitFor()
    assert.ok(metaCount > beforeUninitializedReload, 'uninitialized reload must recheck meta')
    assert.equal(new URL(page.url()).hash, '#/setup')
    console.log(`PASS ${mode}: uninitialized reload rechecked meta and opened setup`)
  } finally {
    await page.close()
  }
}

;(async () => {
  const browser = await chromium.launch({ headless: true })
  try {
    await checkValidation(browser)
    await checkNavigation(browser, 'delayed')
    await checkNavigation(browser, 'failure')
  } finally {
    await browser.close()
  }
})().catch(error => {
  console.error(error)
  process.exitCode = 1
})

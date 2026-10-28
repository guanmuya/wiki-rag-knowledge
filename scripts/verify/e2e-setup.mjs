/**
 * k63 首次引导：真机 Electron 端到端验证。
 *
 * 用 Playwright 驱动真实的打包产物（release/win-unpacked/企业知识库.exe），
 * 走「空数据目录 → 自动跳 #/setup → 填表 → 建号 → 跳登录 → 登录成功」全流程。
 *
 * 关键环境处理：
 * - 必须删掉 ELECTRON_RUN_AS_NODE，否则 Electron 退化成纯 Node，窗口起不来；
 * - 用 --user-data-dir 指到临时目录，绝不碰 %APPDATA%\wiki-rag-desktop；
 * - 打包后的 exe 会自己起 sidecar，数据落在 user-data-dir/data。
 *
 * 用法：node .tmp-verify/e2e-setup.mjs [exePath]
 */
import { _electron as electron } from 'playwright'
import { existsSync, mkdirSync, rmSync, readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'

const ROOT = resolve(import.meta.dirname, '../..')
const EXE = process.argv[2] || join(ROOT, 'frontend', 'release', 'win-unpacked', '企业知识库.exe')
const UD = join(ROOT, '.tmp-verify', 'e2e-userdata')

const results = []
const check = (name, ok, detail) => {
  results.push({ name, ok, detail })
  console.log(`${ok ? '[PASS]' : '[FAIL]'} ${name}`)
  if (detail) console.log(`        ${detail}`)
}

if (!existsSync(EXE)) {
  console.error(`找不到可执行文件：${EXE}`)
  process.exit(1)
}
rmSync(UD, { recursive: true, force: true })
mkdirSync(UD, { recursive: true })
console.log(`exe       : ${EXE}`)
console.log(`userData  : ${UD}\n`)

const env = { ...process.env }
delete env.ELECTRON_RUN_AS_NODE

const app = await electron.launch({
  executablePath: EXE,
  args: [`--user-data-dir=${UD}`],
  env,
  timeout: 120000,
})

let failed = false
try {
  const win = await app.firstWindow({ timeout: 90000 })
  await win.waitForLoadState('domcontentloaded')

  // ---- 1. 自动跳到引导页 ----
  // BackendGate 拿到 needs_setup=true 后会 <Navigate to="/setup">。
  // HashRouter 下地址形如 file:///...index.html#/setup
  await win.waitForFunction(() => location.hash.includes('/setup'), { timeout: 45000 })
  check('1. 空数据目录启动 → 自动跳到 #/setup', true, `hash = ${await win.evaluate(() => location.hash)}`)

  const heading = await win.locator('h1').first().innerText()
  check('2. 引导页文案是「先创建一个管理员账号」', heading.includes('先创建一个管理员账号'), `h1 = ${heading}`)

  // 企业名 / 用户名 输入框（按 label 找，避免依赖 DOM 顺序）
  const orgInput = win.locator('label:has-text("企业名称") + input, input').first()
  const usernameInput = win.locator('input[autocomplete="username"]')
  const passwordInput = win.locator('input[autocomplete="new-password"]').first()
  const confirmInput = win.locator('input[autocomplete="new-password"]').nth(1)

  check('3. 引导页表单齐全（企业名/用户名/密码/确认密码）',
    (await usernameInput.count()) === 1 && (await passwordInput.count()) === 1 &&
    (await confirmInput.count()) === 1,
    `username=${await usernameInput.count()} pwd=${await passwordInput.count()} confirm=${await confirmInput.count()}`)

  // ---- 2. 弱密码被拒（前端拦截，中文提示） ----
  await usernameInput.fill('admin')
  await passwordInput.fill('abc123')
  await confirmInput.fill('abc123')
  await win.locator('button[type="submit"]').click()
  await win.waitForTimeout(600)
  const weakErr = await win.locator('.login-error').innerText().catch(() => '')
  check('4. 弱密码提交 → 页面内中文报错，未跳转',
    weakErr.includes('密码') && (await win.evaluate(() => location.hash)).includes('/setup'),
    `error = ${weakErr}`)

  // ---- 3. 合法表单 → 建号 ----
  const strongPwd = 'WrK-Admin-2026!'
  await passwordInput.fill(strongPwd)
  await confirmInput.fill(strongPwd)
  const displayNameInput = win.locator('input[placeholder*="张管理"]')
  if (await displayNameInput.count()) await displayNameInput.fill('张管理')
  const orgField = win.locator('input[placeholder*="科技有限公司"]')
  if (await orgField.count()) await orgField.fill('某某科技有限公司')

  await win.locator('button[type="submit"]').click()

  // 建号成功 → notifySetupCompleted() → navigate('/login')
  await win.waitForFunction(() => location.hash.includes('/login'), { timeout: 30000 })
  check('5. 提交合法表单 → 跳到 #/login（引导页不再弹回）', true,
    `hash = ${await win.evaluate(() => location.hash)}`)

  // ---- 4. 登录 ----
  await win.locator('input[autocomplete="username"]').fill('admin')
  await win.locator('input[type="password"]').first().fill(strongPwd)
  await win.locator('button[type="submit"]').click()

  // 登录成功进入 /kbs
  await win.waitForFunction(() => !location.hash.includes('/login'), { timeout: 30000 })
  const afterLogin = await win.evaluate(() => location.hash)
  check('6. 用刚建的账号登录 → 进入系统', afterLogin.includes('/kbs'), `hash = ${afterLogin}`)

  // 企业名是登录后 Layout 异步拉的（GET /api/setup/org），
  // 立刻读 innerText 会读到兜底的产品名，等它落位再判定。
  const orgShown = await win
    .waitForFunction(() => document.body.innerText.includes('某某科技有限公司'), { timeout: 8000 })
    .then(() => true)
    .catch(() => false)
  const bodyText = await win.evaluate(() => document.body.innerText)
  check('7. 侧边栏显示企业名（org_name 落库生效）',
    orgShown,
    bodyText.split('\n').slice(0, 6).join(' | '))

  // ---- 5. 已初始化后访问 #/setup 应被弹回 ----
  await win.evaluate(() => { location.hash = '#/setup' })
  await win.waitForTimeout(2500)
  const backFrom = await win.evaluate(() => location.hash)
  // 已登录用户被 LoginPage 直接送回 /kbs 也算正确：关键是不能再停在引导页
  check('8. 初始化后再访问 #/setup → 被弹回（不停在引导页）',
    !backFrom.includes('/setup'), `hash = ${backFrom}`)

  // ---- 6. 关于页：新增的安装位置 / 版本 / 许可证 ----
  await win.evaluate(() => { location.hash = '#/about' })
  await win.waitForTimeout(2500)
  const aboutText = await win.evaluate(() => document.body.innerText)
  const hasInstall = aboutText.includes('安装位置')
  const hasElectron = /Electron 版本/.test(aboutText) && /v3[0-9]\./.test(aboutText)
  const hasLicense = aboutText.includes('许可证与致谢')
  const hasLogDir = aboutText.includes('日志目录')
  check('9. 关于页有「安装位置」', hasInstall, aboutText.match(/安装位置[^\n]*\n?[^\n]*/)?.[0] ?? '')
  check('10. 关于页有 Electron 版本号', hasElectron,
    aboutText.match(/Electron 版本\n[^\n]*/)?.[0] ?? '')
  check('11. 关于页有「日志目录」', hasLogDir, '')
  check('12. 关于页有「许可证与致谢」', hasLicense, '')

  // 展开第三方清单
  const expand = win.locator('button:has-text("展开第三方清单")')
  if (await expand.count()) {
    await expand.click()
    await win.waitForTimeout(500)
    const t = await win.evaluate(() => document.body.innerText)
    check('13. 第三方组件清单可展开', t.includes('DOMPurify') && t.includes('FastAPI'),
      `包含 ${(t.match(/MIT|BSD-3-Clause|Apache-2.0/g) || []).length} 处许可证标识`)
  } else {
    check('13. 第三方组件清单可展开', false, '找不到「展开第三方清单」按钮')
  }

  // ---- 7. 重复建号必须 409（走真实 HTTP） ----
  const apiBase = await win.evaluate(async () => (await window.wrkDesktop.getInfo()).apiBase)
  const dup = await win.evaluate(async (base) => {
    const r = await fetch(`${base}/api/setup/admin`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'hacker', password: 'Another-Pass-2026!' }),
    })
    return { status: r.status, body: await r.text() }
  }, apiBase)
  check('14. 重复 POST /api/setup/admin → 409',
    dup.status === 409 && dup.body.includes('系统已初始化'),
    `HTTP ${dup.status} ${dup.body}`)

  const meta = await win.evaluate(async (base) => {
    const r = await fetch(`${base}/api/meta`)
    return r.json()
  }, apiBase)
  check('15. /api/meta → needs_setup=false / initialized=true',
    meta.needs_setup === false && meta.initialized === true,
    `needs_setup=${meta.needs_setup} initialized=${meta.initialized} org_name=${meta.org_name}`)

  // ---- 8. 数据落在临时 user-data-dir，不是 AppData ----
  const dbPath = join(UD, 'data', 'db', 'app.sqlite3')
  check('16. 数据写在临时 userData（未污染 %APPDATA%）', existsSync(dbPath), dbPath)

  // 截图留证
  mkdirSync(join(ROOT, '.tmp-verify', 'shots'), { recursive: true })
  await win.evaluate(() => { location.hash = '#/about' })
  await win.waitForTimeout(1200)
  await win.screenshot({ path: join(ROOT, '.tmp-verify', 'shots', 'about-desktop.png'), fullPage: true })
} catch (e) {
  failed = true
  console.error('\n[EXCEPTION]', e.message)
  try {
    const win = await app.firstWindow()
    console.error('当前 hash:', await win.evaluate(() => location.hash))
    console.error('页面文本:', (await win.evaluate(() => document.body.innerText)).slice(0, 800))
  } catch { /* 窗口可能已关 */ }
} finally {
  await app.close().catch(() => {})
}

const passed = results.filter((r) => r.ok).length
console.log(`\n=== ${passed}/${results.length} 通过 ===`)
for (const r of results) console.log(`  ${r.ok ? 'PASS' : 'FAIL'}  ${r.name}`)

const logPath = join(UD, 'logs', 'main.log')
if (existsSync(logPath)) {
  const log = readFileSync(logPath, 'utf8')
  console.log('\n--- 主进程日志（末 12 行）---')
  console.log(log.trim().split('\n').slice(-12).join('\n'))
}

process.exit(passed === results.length && !failed ? 0 : 1)

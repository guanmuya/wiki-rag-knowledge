/**
 * 回归：弹窗内输入框焦点被抢的缺陷（输入一个字符后就打不进字）。
 *
 * 根因：`ui.tsx` 的 Modal 把 `onClose`（内联箭头函数，每次 render 都是新身份）
 * 放进了 focus effect 的依赖里 —— 每敲一个字 → 父组件 re-render → effect 重跑
 * → `ref.current.focus()` 把焦点从输入框抢到弹窗容器。
 *
 * 用法（跑已打包的 win-unpacked，必须清掉 ELECTRON_RUN_AS_NODE）：
 *   cd frontend
 *   env -u ELECTRON_RUN_AS_NODE node scripts/verify-modal-input.mjs [exePath]
 *
 * 验收：8/8 通过。在 1.0.1 旧包上跑同一脚本会失败（Base URL 只剩 1 个字符），
 * 可作为「修复前 → 修复后」的对照。
 */
import { _electron as electron } from 'playwright'
import { rmSync, mkdirSync } from 'node:fs'
import { join, resolve } from 'node:path'

const ROOT = resolve(import.meta.dirname, '../..')
const EXE = process.argv[2] || join(ROOT, 'frontend', 'release', 'win-unpacked', '企业知识库.exe')
const UD = join(ROOT, '.tmp-verify', 'input-userdata')
const PASSWORD = 'AdminTest#2026'

const env = { ...process.env }
delete env.ELECTRON_RUN_AS_NODE

rmSync(UD, { recursive: true, force: true })
mkdirSync(UD, { recursive: true })

const results = []
const check = (name, ok, detail) => {
  results.push(ok)
  console.log(`${ok ? '[PASS]' : '[FAIL]'} ${name}${detail ? ` — ${detail}` : ''}`)
}

const app = await electron.launch({
  executablePath: EXE,
  args: [`--user-data-dir=${UD}`],
  env,
  timeout: 120000,
})

try {
  const win = await app.firstWindow({ timeout: 90000 })
  await win.waitForTimeout(3500)

  // 1) 走首次引导建号（用引导页建的 admin 没有「必须改密码」标记，可直接登录）
  await win.waitForFunction(() => location.hash.includes('/setup'), { timeout: 60000 })
  const usernameInput = win.locator('input[autocomplete="username"]')
  const passwordInput = win.locator('input[autocomplete="new-password"]').first()
  const confirmInput = win.locator('input[autocomplete="new-password"]').nth(1)
  await usernameInput.fill('admin')
  await passwordInput.fill(PASSWORD)
  await confirmInput.fill(PASSWORD)
  const displayNameInput = win.locator('input[placeholder*="张管理"]')
  if (await displayNameInput.count()) await displayNameInput.fill('张管理')
  const orgField = win.locator('input[placeholder*="科技有限公司"]')
  if (await orgField.count()) await orgField.fill('某某科技有限公司')
  await win.locator('button[type="submit"]').click()
  await win.waitForFunction(() => location.hash.includes('/login'), { timeout: 30000 })

  await win.locator('input[autocomplete="username"]').fill('admin')
  await win.locator('input[type="password"]').first().fill(PASSWORD)
  await win.locator('button[type="submit"]').click()
  await win.waitForFunction(() => !location.hash.includes('/login'), { timeout: 30000 })
  await win.waitForTimeout(800)

  // 2) 进「模型设置」
  await win.getByRole('link', { name: '模型设置' }).click()
  await win.waitForTimeout(900)
  check('进入模型设置页', await win.getByRole('button', { name: '+ 供应商' }).isVisible())

  // 3) 添加供应商：逐个字符输入，焦点不能被抢
  await win.getByRole('button', { name: '+ 供应商' }).click()
  await win.waitForTimeout(400)
  const modal = win.locator('.modal')
  const nameInput = modal.getByLabel('名称')
  await nameInput.click()
  const NAME = '测试供应商ABC'
  await nameInput.pressSequentially(NAME, { delay: 60 })
  const nameValue = await nameInput.inputValue()
  check('供应商名称逐字输入完整保留', nameValue === NAME, `实际="${nameValue}"`)

  const baseInput = modal.getByLabel('Base URL')
  await baseInput.click()
  const BASE = 'https://api.example.com/v1'
  await baseInput.pressSequentially(BASE, { delay: 25 })
  const baseValue = await baseInput.inputValue()
  check('Base URL 逐字输入完整保留', baseValue === BASE, `实际="${baseValue}"`)

  const keyInput = modal.getByLabel('API Key')
  await keyInput.click()
  const KEY = 'sk-test-123456'
  await keyInput.pressSequentially(KEY, { delay: 40 })
  check('API Key 逐字输入完整保留', (await keyInput.inputValue()) === KEY)

  await modal.getByRole('button', { name: '保存' }).click()
  await win.waitForTimeout(1500)
  check('保存后供应商落库', (await win.locator('.modal').count()) === 0 &&
    (await win.getByText('测试供应商ABC').count()) > 0)

  // 4) 添加模型：同一个 Modal 组件，再验一次
  await win.getByRole('button', { name: '+ 模型' }).click()
  await win.waitForTimeout(400)
  const modal2 = win.locator('.modal')
  const modelInput = modal2.locator('input[placeholder="gpt-4o-mini"]')
  await modelInput.click()
  const MODEL = 'gpt-test-4o-mini'
  await modelInput.pressSequentially(MODEL, { delay: 45 })
  check('模型名逐字输入完整保留', (await modelInput.inputValue()) === MODEL,
    `实际="${await modelInput.inputValue()}"`)

  const labelInput = modal2.locator('input[placeholder*="主对话模型"]')
  await labelInput.click()
  const LABEL = '主对话模型'
  await labelInput.pressSequentially(LABEL, { delay: 60 })
  check('显示名逐字输入完整保留', (await labelInput.inputValue()) === LABEL,
    `实际="${await labelInput.inputValue()}"`)

  // 焦点必须在最后一个输入框里，而不是被抢回弹窗容器
  const focused = await win.evaluate(() => {
    const el = document.activeElement
    return { tag: el?.tagName, value: el?.value ?? '' }
  })
  check('焦点仍在输入框内', focused.tag === 'INPUT' && focused.value === LABEL,
    JSON.stringify(focused))
} finally {
  await app.close()
}

const failed = results.filter((x) => !x).length
console.log(`\n=== ${results.length - failed}/${results.length} 通过 ===`)
process.exit(failed ? 1 : 0)

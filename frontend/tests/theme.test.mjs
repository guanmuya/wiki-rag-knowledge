/**
 * 外观主题（跟随系统 / 浅色 / 深色）的回归测试。
 *
 * 为什么值得单独一个文件：`tests/e2e.test.mjs` 的 5 条用例是发布口径里的「E2E 5/5」，
 * 往里塞新用例会让这个数字失去意义（见 tests/README.md §4）。这里同样用
 * 真实 Chromium + 真实 Vite + 内存 mock 后端，只是换一个端口和缓存目录。
 *
 * 覆盖的是这次改动里最容易悄悄退化的四件事：
 *  1. **浅色不回退**：令牌取值逐项钉死（用户给的基准截图就是现状）；
 *  2. **深色真的落地**：data-theme / color-scheme / 令牌 / 实色按钮 / 下拉箭头（含禁用态）；
 *  3. **跟随系统是实时的**：系统改深浅后不刷新就重算；
 *  4. **首屏不闪白**：不执行 React 时 `public/theme-boot.js` 也能把深色写上去。
 * 另外还有一组纯 Node 断言，锁住「主进程偏好文件」和「三处底色常量必须一致」。
 */
import { after, before, describe, test } from 'node:test'
import assert from 'node:assert/strict'
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { chromium } from 'playwright'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
// 与 e2e.test.mjs 的 15173、usage-page.test.mjs 的 15174 分开：
// `node --test` 会并行跑这些文件，各自要自己的端口与 Vite 缓存目录。
const PORT = Number(process.env.THEME_E2E_PORT || 15175)
const BASE = `http://127.0.0.1:${PORT}`
const NOW = '2026-01-01T00:00:00Z'
// 只进 mock 后端的假令牌：没有任何真实凭据。
const TOKEN = '{{SECRET_th3m3t0k3n}}'

const require = createRequire(import.meta.url)
const prefs = require('../electron/theme-prefs.cjs')

const USER = {
  id: 1, username: 'admin', display_name: '系统管理员', role: 'admin',
  is_active: true, must_change_password: false, created_at: NOW,
}

const json = async (route, body, status = 200) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

/** 极简 mock：只需要让 Layout 与知识库列表渲染出来，其余一律 200 {}。 */
async function handleApi(route) {
  const path = new URL(route.request().url()).pathname
  if (path === '/api/auth/me') {
    return json(route, { user: USER, permissions: { is_admin: true, kb_grants: [] } })
  }
  if (path === '/api/kbs') return json(route, { items: [], total: 0 })
  return json(route, {})
}

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
    await new Promise((pause) => setTimeout(pause, 50))
  }
  throw new Error(`等待超时：${label}${lastError ? `（${lastError.message}）` : ''}`)
}

/** 读文档当前的外观状态：属性、color-scheme、几个关键令牌、落盘的选择。 */
const themeState = (page) => page.evaluate(() => {
  const style = getComputedStyle(document.documentElement)
  const token = (name) => style.getPropertyValue(name).trim()
  return {
    attr: document.documentElement.getAttribute('data-theme'),
    colorScheme: style.colorScheme,
    htmlBackground: style.backgroundColor,
    saved: localStorage.getItem('wrk_appearance'),
    meta: document.querySelector('meta[name="theme-color"]')?.getAttribute('content'),
    tokens: {
      bg: token('--c-bg'),
      surface: token('--c-surface'),
      text: token('--c-text'),
      primary: token('--c-primary'),
      border: token('--c-border'),
      surfaceAlt: token('--c-surface-alt'),
      warn: token('--c-warn'),
    },
  }
})

/**
 * 组件级断言用的最小样本：主题是样式层的东西，断言要落在确定的元素上，
 * 不去依赖某个页面的具体内容（页面文案随时会改）。样本挂在视口外，不影响截图与布局。
 */
const FIXTURE = `
  <div id="theme-fixture" style="position:fixed;left:-9999px;top:0">
    <button class="btn btn-primary" id="fx-primary">主按钮</button>
    <input class="input" id="fx-input" />
    <select class="select" id="fx-select"><option>一</option></select>
    <select class="select" id="fx-select-disabled" disabled><option>一</option></select>
    <span class="badge badge-warn" id="fx-badge">待校对</span>
  </div>`

const fixtureStyles = (page) => page.evaluate((html) => {
  document.getElementById('theme-fixture')?.remove()
  document.body.insertAdjacentHTML('beforeend', html)
  const read = (id, props) => {
    const style = getComputedStyle(document.getElementById(id))
    return Object.fromEntries(props.map((prop) => [prop, style[prop]]))
  }
  const result = {
    primary: read('fx-primary', ['backgroundColor', 'color']),
    input: read('fx-input', ['backgroundColor']),
    select: read('fx-select', ['backgroundColor', 'backgroundImage']),
    // 禁用态：03 用 `background` 简写铺 sunken 底，会顺手把箭头清掉。
    // 深色分片不许把箭头补回来（简写已把 repeat 重置成 repeat，补了就成一排平铺箭头），
    // 也不许把 sunken 底抬成可编辑的底色。
    selectDisabled: read('fx-select-disabled', [
      'backgroundColor', 'backgroundImage', 'backgroundRepeat', 'color',
    ]),
    badge: read('fx-badge', ['backgroundColor', 'color']),
  }
  document.getElementById('theme-fixture')?.remove()
  return result
}, FIXTURE)

// ---------------------------------------------------------------------------
// A. 纯 Node：主进程偏好文件、三处常量、首屏脚本的接线
// ---------------------------------------------------------------------------

describe('外观：主进程偏好与首屏脚本（纯 Node）', () => {
  test('偏好文件：缺文件 / 坏 JSON / 未知取值都回落到跟随系统', () => {
    const dir = mkdtempSync(join(tmpdir(), 'wrk-theme-'))
    try {
      assert.equal(prefs.readMode(dir), 'system', '首次使用：没有文件就是跟随系统')
      assert.equal(prefs.writeMode(dir, 'dark'), 'dark', 'writeMode 返回规范化后的档位')
      assert.equal(prefs.readMode(dir), 'dark', '写进去的档位要能读回来')
      writeFileSync(join(dir, prefs.FILE_NAME), '{ 这不是 JSON', 'utf8')
      assert.equal(prefs.readMode(dir), 'system', '文件损坏不能让应用起不来')
      writeFileSync(join(dir, prefs.FILE_NAME), JSON.stringify({ mode: 'blue' }), 'utf8')
      assert.equal(prefs.readMode(dir), 'system', '不认识的取值按跟随系统')
      assert.equal(prefs.writeMode(dir, 'blue'), 'system', '写入前也要先规范化')
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  test('窗口底色：三档 × 系统明暗', () => {
    const { BACKGROUND } = prefs
    assert.equal(prefs.resolveBackground('system', true), BACKGROUND.dark)
    assert.equal(prefs.resolveBackground('system', false), BACKGROUND.light)
    assert.equal(prefs.resolveBackground('dark', false), BACKGROUND.dark, '深色档不看系统设置')
    assert.equal(prefs.resolveBackground('light', true), BACKGROUND.light, '浅色档不看系统设置')
    // 浅色档必须还是改动前的窗口底色：窗口第一帧与截图基准一致
    assert.equal(BACKGROUND.light, '#f5f7fa')
    // 深色档与 12-theme.css 的深色 --c-bg 同色，启动第一帧不跳色
    assert.equal(BACKGROUND.dark, '#0f1116')
  })

  test('首屏脚本、主题模块、主进程三处的键与底色一致（改一处必须改两处）', () => {
    const boot = readFileSync(join(ROOT, 'public/theme-boot.js'), 'utf8')
    const theme = readFileSync(join(ROOT, 'src/theme.tsx'), 'utf8')
    const main = readFileSync(join(ROOT, 'electron/theme-prefs.cjs'), 'utf8')

    const key = /(?:const|var) KEY = '([^']+)'/.exec(boot)?.[1]
    assert.equal(key, 'wrk_appearance', '首屏脚本里的 storage 键')
    assert.ok(
      theme.includes(`THEME_STORAGE_KEY = '${key}'`),
      'src/theme.tsx 必须用同一个 storage 键，否则首屏与运行时会各读一份',
    )

    assert.ok(boot.includes("var DARK_DOC_BACKGROUND = '#0f1116'"), '首屏脚本的深色文档底色')
    assert.ok(theme.includes("THEME_DARK_DOC_BACKGROUND = '#0f1116'"), 'theme.tsx 的深色文档底色')
    assert.ok(boot.includes("light: '#f5f7fa'") && theme.includes("light: '#f5f7fa'"))
    assert.ok(main.includes("light: '#f5f7fa'"), '主进程窗口底色与 chrome 底色一致')
    for (const source of [boot, theme, main]) {
      assert.ok(source.includes('#0f1116'), '三处的深色底色必须相同')
    }
    // 浅色不许写内联文档底色：html 保持透明，浅色观感一个像素都不变
    assert.ok(!boot.includes("'#ffffff'"), '首屏脚本不该往浅色文档上刷底色')
    assert.ok(!theme.includes("'#ffffff'"), 'theme.tsx 不该往浅色文档上刷底色')
  })

  test('index.html 用外部脚本在样式表与入口之前落地主题（CSP 不允许内联）', () => {
    const html = readFileSync(join(ROOT, 'index.html'), 'utf8')
    const bootAt = html.indexOf('theme-boot.js')
    assert.ok(bootAt > 0, 'index.html 必须引用 theme-boot.js')
    assert.ok(bootAt < html.indexOf('src/main.tsx'), '首屏脚本要排在入口脚本之前')
    assert.match(html, /<script src="\/theme-boot\.js"><\/script>/, '必须是外部普通脚本（CSP script-src self）')
    assert.match(html, /script-src 'self'/, 'CSP 不能被放宽成允许内联')
    assert.ok(!html.includes('wrk_appearance'), '主题逻辑不能内联进 index.html')
    assert.ok(existsSync(join(ROOT, 'public/theme-boot.js')), 'public/theme-boot.js 存在才会被 vite 拷进 dist')
  })
})

// ---------------------------------------------------------------------------
// B. 浏览器：真实 Chromium + 真实 Vite
// ---------------------------------------------------------------------------

let viteServer
let browser

describe('外观：深色 / 浅色 / 跟随系统（真实浏览器）', () => {
  before(async () => {
    viteServer = await createServer({
      root: ROOT,
      configFile: resolve(ROOT, 'vite.config.ts'),
      base: '/',
      logLevel: 'error',
      // 独立缓存目录：与另外两个浏览器测试文件并行跑时不抢依赖预构建结果。
      cacheDir: resolve(ROOT, 'node_modules/.vite-theme'),
      server: { host: '127.0.0.1', port: PORT, strictPort: true },
    })
    await viteServer.listen()
    browser = await chromium.launch({ headless: true })
  })

  after(async () => {
    await browser?.close()
    await viteServer?.close()
  })

  /**
   * 打开应用并停在知识库列表。
   * `saved` 为 null 时不写 localStorage，用来验「默认跟随系统」。
   */
  async function openApp({ colorScheme = 'light', saved = null } = {}) {
    const context = await browser.newContext({
      viewport: { width: 1440, height: 900 }, locale: 'zh-CN', colorScheme,
    })
    await context.addInitScript(([token, mode]) => {
      localStorage.setItem('wrk_token', token)
      if (mode) localStorage.setItem('wrk_appearance', mode)
    }, [TOKEN, saved])
    await context.route('**/api/**', (route) => handleApi(route))
    const page = await context.newPage()
    const errors = []
    page.on('pageerror', (err) => errors.push(err.message))
    const missing = []
    page.on('response', (response) => {
      if (response.status() >= 400) missing.push(`${response.status()} ${response.url()}`)
    })
    await page.goto(`${BASE}/#/kbs`, { waitUntil: 'domcontentloaded' })
    await page.locator('.theme-switch-item').first().waitFor({ timeout: 20000 })
    return { context, page, errors, missing }
  }

  const clickMode = async (page, label) => {
    await page.locator('.theme-switch-item', { hasText: label }).first().click()
  }

  test('默认跟随系统：系统深色即深色，color-scheme 同时为 dark', { timeout: 60000 }, async () => {
    const { context, page, errors, missing } = await openApp({ colorScheme: 'dark' })
    try {
      const state = await waitFor(
        async () => {
          const current = await themeState(page)
          return current.attr === 'dark' ? current : null
        },
        '系统深色时默认就是深色',
      )
      assert.equal(state.saved, null, '默认档位不写 localStorage（没点过就是没点过）')
      // color-scheme 让滚动条、<select> 弹层这些原生控件跟着变，光给 body 上色没用
      assert.equal(state.colorScheme, 'dark')
      assert.equal(state.htmlBackground, 'rgb(15, 17, 22)', '文档底色是深色，首帧不会先白一下')
      assert.equal(state.meta, '#0f1116', 'theme-color 跟着主题走')
      assert.equal(state.tokens.bg, '#0f1116')
      assert.equal(state.tokens.surface, '#171a21')
      assert.equal(state.tokens.text, '#e8edf6')

      // 开关的当前档位：跟随系统（aria-pressed 三选一）
      assert.equal(await page.locator('.theme-switch-item[aria-pressed="true"]').innerText(), '跟随系统')
      // 首屏脚本、样式分片、入口脚本都是真实加载的：这里不能有 404
      assert.deepEqual(missing, [])
      assert.deepEqual(errors, [])
    } finally {
      await context.close()
    }
  })

  test('首屏脚本独立生效：不执行 React 也已经是深色（防白闪）', { timeout: 60000 }, async () => {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: 'light' })
    await context.addInitScript(() => localStorage.setItem('wrk_appearance', 'dark'))
    // 掐掉入口脚本：剩下的只有 index.html 里的 theme-boot.js 和它写下的属性。
    await context.route('**/src/main.tsx*', (route) => route.abort())
    const page = await context.newPage()
    try {
      await page.goto(`${BASE}/`, { waitUntil: 'load' })
      const info = await page.evaluate(() => ({
        attr: document.documentElement.getAttribute('data-theme'),
        inline: document.documentElement.style.colorScheme,
        background: getComputedStyle(document.documentElement).backgroundColor,
        meta: document.querySelector('meta[name="theme-color"]').getAttribute('content'),
        root: document.getElementById('root').childElementCount,
      }))
      assert.equal(info.attr, 'dark', 'React 没跑起来也要是深色')
      assert.equal(info.inline, 'dark', 'color-scheme 由首屏脚本写入')
      assert.equal(info.background, 'rgb(15, 17, 22)')
      assert.equal(info.meta, '#0f1116')
      assert.equal(info.root, 0, '确认 React 确实没渲染（否则这条用例证明不了首屏脚本）')
    } finally {
      await context.close()
    }
  })

  test('切到浅色：令牌逐项回到改动前的取值', { timeout: 60000 }, async () => {
    const { context, page, errors } = await openApp({ colorScheme: 'dark', saved: 'dark' })
    try {
      await clickMode(page, '浅色')
      const state = await waitFor(
        async () => {
          const current = await themeState(page)
          return current.attr === 'light' ? current : null
        },
        '点浅色后切成浅色',
      )
      assert.equal(state.saved, 'light', '选择要落 localStorage')
      assert.equal(state.colorScheme, 'light')
      // 浅色不写内联文档底色：html 与改动前一样是透明的，由 body 画布绘制
      assert.equal(state.htmlBackground, 'rgba(0, 0, 0, 0)')
      assert.equal(state.meta, '#f5f7fa')
      // 浅色基准：这几项就是用户截图里的现状，改动不得
      assert.deepEqual(state.tokens, {
        bg: '#ffffff',
        surface: '#ffffff',
        text: '#142338',
        primary: '#2563d6',
        border: '#dce5ef',
        surfaceAlt: '#f8fafc',
        warn: '#a16207',
      })
      // 组件级也回到浅色：主按钮原色、输入框纯白、下拉箭头还在
      const styles = await fixtureStyles(page)
      assert.deepEqual(styles.primary, { backgroundColor: 'rgb(37, 99, 214)', color: 'rgb(255, 255, 255)' })
      assert.equal(styles.input.backgroundColor, 'rgb(255, 255, 255)')
      assert.match(styles.select.backgroundImage, /svg/, '浅色下拉箭头不能被深色覆盖弄丢')
      assert.equal(styles.selectDisabled.backgroundImage, 'none', '浅色禁用下拉本来就没有箭头')
      assert.deepEqual(errors, [])
    } finally {
      await context.close()
    }
  })

  test('刷新后保持选择：深色与浅色都能记住', { timeout: 60000 }, async () => {
    const { context, page } = await openApp({ colorScheme: 'light' })
    try {
      await clickMode(page, '深色')
      await waitFor(async () => (await themeState(page)).attr === 'dark', '切到深色')
      await page.reload({ waitUntil: 'domcontentloaded' })
      await page.locator('.theme-switch-item').first().waitFor({ timeout: 20000 })
      const dark = await waitFor(
        async () => {
          const current = await themeState(page)
          return current.attr === 'dark' ? current : null
        },
        '刷新后仍是深色',
      )
      assert.equal(dark.saved, 'dark')
      assert.equal(await page.locator('.theme-switch-item[aria-pressed="true"]').innerText(), '深色')

      await clickMode(page, '浅色')
      await waitFor(async () => (await themeState(page)).attr === 'light', '切回浅色')
      await page.reload({ waitUntil: 'domcontentloaded' })
      await page.locator('.theme-switch-item').first().waitFor({ timeout: 20000 })
      assert.equal(await waitFor(async () => (await themeState(page)).attr === 'light', '刷新后仍是浅色'), true)
    } finally {
      await context.close()
    }
  })

  test('跟随系统档实时跟随：系统换深浅后不刷新就重算', { timeout: 60000 }, async () => {
    const { context, page } = await openApp({ colorScheme: 'light', saved: 'dark' })
    try {
      // 先切回「跟随系统」：此时系统是浅色，界面应该变浅
      await clickMode(page, '跟随系统')
      const first = await waitFor(
        async () => {
          const current = await themeState(page)
          return current.attr === 'light' ? current : null
        },
        '跟随系统档下系统浅色即浅色',
      )
      assert.equal(first.saved, 'system')

      // 系统切到深色：不刷新、不重新加载，data-theme 必须跟着变
      await page.emulateMedia({ colorScheme: 'dark' })
      const second = await waitFor(
        async () => {
          const current = await themeState(page)
          return current.attr === 'dark' ? current : null
        },
        '系统切深色后界面实时变深',
      )
      assert.equal(second.htmlBackground, 'rgb(15, 17, 22)')

      // 系统切回浅色：同样不需要刷新
      await page.emulateMedia({ colorScheme: 'light' })
      assert.equal(
        await waitFor(async () => (await themeState(page)).attr === 'light', '系统切浅色后界面实时变浅'),
        true,
      )
    } finally {
      await context.close()
    }
  })

  test('深色下的关键组件：实色按钮、输入框抬高一档、箭头在而禁用态不跟着抬', { timeout: 60000 }, async () => {
    const { context, page, errors } = await openApp({ colorScheme: 'light', saved: 'dark' })
    try {
      await waitFor(async () => (await themeState(page)).attr === 'dark', '深色生效')
      const styles = await fixtureStyles(page)
      // 主色在深色下当文字用亮档，当实色底必须另给一档更深的，否则白字对比度不够
      assert.deepEqual(styles.primary, { backgroundColor: 'rgb(63, 111, 216)', color: 'rgb(255, 255, 255)' })
      assert.equal(styles.input.backgroundColor, 'rgb(27, 32, 42)', '输入框比卡片亮半档，看得出来能输入')
      assert.equal(styles.select.backgroundColor, 'rgb(27, 32, 42)')
      // 这条是真实回归点：用 background 简写会把下拉箭头的 background-image 清掉
      assert.match(styles.select.backgroundImage, /8b97ab/, '深色下拉箭头必须还在')
      // 禁用下拉不许被补箭头：03 的 `:disabled` 简写把 repeat 重置成了 repeat，
      // 补上就是平铺一整排箭头（问答页离线时模型下拉正是这个状态）
      assert.equal(styles.selectDisabled.backgroundImage, 'none', '禁用下拉不该有箭头')
      assert.equal(styles.selectDisabled.backgroundRepeat, 'repeat', '（简写留下的 repeat，所以不能补）')
      assert.equal(styles.selectDisabled.backgroundColor, 'rgb(31, 36, 46)', '禁用态保持 sunken 底，不跟着抬档')
      assert.equal(styles.selectDisabled.color, 'rgb(139, 151, 171)', '禁用态文字用弱化色')
      assert.deepEqual(styles.badge, { backgroundColor: 'rgb(43, 34, 19)', color: 'rgb(224, 163, 74)' })

      // 整页没有 404（首屏脚本、样式分片都是真实加载的）
      assert.deepEqual(errors, [])
    } finally {
      await context.close()
    }
  })

  test('登录页也跟随外观（没有侧栏、没有开关，但主题一致）', { timeout: 60000 }, async () => {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN', colorScheme: 'light' })
    await context.addInitScript(() => localStorage.setItem('wrk_appearance', 'dark'))
    await context.route('**/api/**', (route) => handleApi(route))
    const page = await context.newPage()
    try {
      await page.goto(`${BASE}/#/login`, { waitUntil: 'domcontentloaded' })
      await page.getByRole('heading', { name: '登录', exact: true }).waitFor({ timeout: 20000 })
      const state = await themeState(page)
      assert.equal(state.attr, 'dark')
      assert.equal(state.colorScheme, 'dark')
      // 登录页没有侧栏开关：不给出「在登录页切外观」的入口
      assert.equal(await page.locator('.theme-switch').count(), 0)
    } finally {
      await context.close()
    }
  })
})

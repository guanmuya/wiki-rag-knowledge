#!/usr/bin/env node
/**
 * 开发模式一键启动：
 *   1. 起 vite dev server（127.0.0.1:5173，strictPort）
 *   2. 轮询等它就绪（HTTP 200）
 *   3. 用 WRK_DEV=1 WRK_DEV_URL=http://localhost:5173 启动 Electron
 *
 * 用法：
 *   node scripts/dev.mjs                # 起 vite + electron
 *   node scripts/dev.mjs --web-only     # 只起 vite（浏览器调试用）
 *   node scripts/dev.mjs --port 5174    # 换端口
 *
 * ⚠️ 本机环境变量 ELECTRON_RUN_AS_NODE=1 会让 Electron 退化成纯 Node 进程：
 *    ipcMain 变 undefined → `Cannot read properties of undefined (reading 'handle')`。
 *    所以下面 spawn electron 前必须 delete env.ELECTRON_RUN_AS_NODE。
 *    手工在终端里起也要 `env -u ELECTRON_RUN_AS_NODE node_modules/.bin/electron .`。
 */
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const FRONTEND = resolve(HERE, '..')
const NODE = process.env.WRK_NODE || process.execPath

const argv = process.argv.slice(2)
const WEB_ONLY = argv.includes('--web-only')
const portIdx = argv.indexOf('--port')
const PORT = portIdx >= 0 ? Number(argv[portIdx + 1]) : 5173
const DEV_URL = `http://localhost:${PORT}`

const log = (...a) => console.log(`[dev] ${a.join(' ')}`)

/** 清掉会破坏 Electron 的环境变量。 */
function electronEnv(extra = {}) {
  const env = { ...process.env, ...extra }
  delete env.ELECTRON_RUN_AS_NODE   // 关键：否则 ipcMain 不可用
  delete env.ELECTRON_NO_ATTACH_CONSOLE
  return env
}

/** 解析 electron 可执行文件：优先 require('electron')（返回 exe 绝对路径）。 */
function resolveElectron() {
  // 1) 标准方式：electron 包的 index.js 导出可执行文件路径
  try {
    const require = createRequire(join(FRONTEND, 'package.json'))
    const p = require('electron')
    if (typeof p === 'string' && existsSync(p)) return p
  } catch { /* 落到下面 */ }

  // 2) path.txt + dist
  const pkgDir = join(FRONTEND, 'node_modules', 'electron')
  const distExe = join(pkgDir, 'dist', process.platform === 'win32' ? 'electron.exe' : 'Electron')
  if (existsSync(distExe)) return distExe

  // 3) .bin 垫片
  const bin = join(FRONTEND, 'node_modules', '.bin',
    process.platform === 'win32' ? 'electron.cmd' : 'electron')
  if (existsSync(bin)) return bin

  return null
}

const children = []
let shuttingDown = false

function shutdown(code = 0) {
  if (shuttingDown) return
  shuttingDown = true
  log('正在关闭...')
  for (const c of children) {
    try { c.kill() } catch { /* 已退出 */ }
  }
  setTimeout(() => process.exit(code), 500)
}
process.on('SIGINT', () => shutdown(0))
process.on('SIGTERM', () => shutdown(0))

/** 等 vite 就绪：轮询 HTTP，直到 200 或超时。 */
async function waitForServer(url, timeoutMs = 60000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(2000) })
      if (res.ok) return true
    } catch { /* 还没起 */ }
    await new Promise((r) => setTimeout(r, 400))
  }
  return false
}

// ---------- 1. vite dev server ----------
const viteJs = join(FRONTEND, 'node_modules', 'vite', 'bin', 'vite.js')
if (!existsSync(viteJs)) {
  console.error(`[dev] 找不到 vite：${viteJs}\n  请先执行：node <pnpm.mjs> install`)
  process.exit(1)
}

log(`启动 vite dev server（${DEV_URL}）...`)
const vite = spawn(NODE, [viteJs, '--port', String(PORT), '--strictPort'], {
  cwd: FRONTEND,
  env: electronEnv(),
  stdio: 'inherit',
  windowsHide: false,
})
children.push(vite)
vite.on('exit', (code) => {
  if (!shuttingDown) {
    log(`vite 退出（code=${code}），关闭全部进程`)
    shutdown(code ?? 0)
  }
})

log('等待 vite 就绪...')
if (!(await waitForServer(DEV_URL))) {
  console.error(`[dev] vite 在 60s 内未就绪（${DEV_URL}）`)
  shutdown(1)
} else {
  log(`vite 就绪：${DEV_URL}`)

  if (WEB_ONLY) {
    log('--web-only：只用浏览器打开上面地址，Ctrl+C 退出')
  } else {
    // ---------- 2. Electron ----------
    const electronPath = resolveElectron()
    if (!electronPath) {
      console.error('[dev] 找不到 electron 可执行文件。\n' +
        '  请执行：node <pnpm.mjs> install\n' +
        '  若已安装但仍缺失，手动补下载：node node_modules/electron/install.js')
      shutdown(1)
    } else {
      log(`启动 Electron：${electronPath}`)
      const electron = spawn(electronPath, ['.'], {
        cwd: FRONTEND,
        env: electronEnv({
          WRK_DEV: '1',
          WRK_DEV_URL: DEV_URL,
          // Electron 主进程默认仍会回退到 .venv 的 python 跑 app.desktop_entry
        }),
        stdio: 'inherit',
        windowsHide: false,
      })
      children.push(electron)
      electron.on('exit', (code) => {
        if (!shuttingDown) {
          log(`Electron 退出（code=${code}），关闭 vite`)
          shutdown(code ?? 0)
        }
      })
    }
  }
}

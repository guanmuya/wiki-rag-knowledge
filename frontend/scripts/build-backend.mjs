#!/usr/bin/env node
/**
 * 用 PyInstaller 把 backend/app/desktop_entry.py 打成单文件 exe。
 *
 * 产物：frontend/build/backend/wrk-backend.exe
 * 中间件：frontend/build/pyinstaller/（launcher、spec、work）
 *
 * 设计要点
 * --------
 * 1. desktop_entry.py 里用的是相对导入（`from .main import app`），
 *    直接把它当脚本喂给 PyInstaller 会让 `__main__` 没有 package，
 *    运行时 `ImportError: attempted relative import with no known parent package`。
 *    所以这里生成一个几行的 launcher（`from app.desktop_entry import main`），
 *    并用 `--paths backend` 把 `app` 包暴露给分析器。
 * 2. 大量三方库有数据文件 / 动态子模块，PyInstaller 静态分析会漏，见 COLLECT_ALL；
 *    依赖与收集清单都由 backend/requirements*.txt 锁定，保证打包可复现。
 * 3. 打完必须实测：拉起 exe → 读 stdout 的 WRK_READY → HTTP 打 /api/health。
 *
 * 用法：
 *   node scripts/build-backend.mjs            # 打包 + 实测
 *   node scripts/build-backend.mjs --fast     # 跳过 --clean，复用 PyInstaller 缓存
 *   node scripts/build-backend.mjs --no-verify# 只打包
 */
import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const FRONTEND = resolve(HERE, '..')
const ROOT = resolve(FRONTEND, '..')

const VENV_PYTHON = process.env.WRK_PYTHON || join(ROOT, '.venv', 'Scripts', 'python.exe')
const BACKEND_DIR = join(ROOT, 'backend')
const ENTRY = join(BACKEND_DIR, 'app', 'desktop_entry.py')

// P1-2：依赖全部由锁文件固定，构建脚本不再自行决定版本或收集范围。
const BUILD_REQS = join(BACKEND_DIR, 'requirements-build.txt')
const CONSTRAINTS = join(BACKEND_DIR, 'constraints.txt')

const BUILD_DIR = join(FRONTEND, 'build')
const PYI_DIR = join(BUILD_DIR, 'pyinstaller')
const DIST_DIR = join(BUILD_DIR, 'backend')
const EXE = join(DIST_DIR, 'wrk-backend.exe')
const LAUNCHER = join(PYI_DIR, 'wrk_launcher.py')
const WORK_DIR = join(PYI_DIR, 'work')
const SPEC_DIR = join(PYI_DIR, 'spec')

const argv = process.argv.slice(2)
const FAST = argv.includes('--fast')
const NO_VERIFY = argv.includes('--no-verify')

/**
 * 需要整体收集（代码 + 数据 + 元数据）的包。
 *
 * - pypdf：解析 PDF，内部按版本分支动态 import，静态分析会漏子模块。
 * - docx (python-docx)：需要 docx/templates/default.docx 作为新文档模板。
 * - pptx (python-pptx)：需要 pptx/templates/default.pptx + 主题资源。
 * - openpyxl：需要 openpyxl/.constants.json 之类元数据文件。
 * - lxml：C 扩展 + .pxd/isoschematron 资源，hook 覆盖不全时缺 xpath 引擎。
 * - cryptography：rust 绑定 + 后端接口，hook 已有但 collect 更保险（Fernet 依赖）。
 * - sqlalchemy：dialect 通过 registry 动态加载，sqlite 方言需显式收集。
 * - uvicorn：loops/protocols/lifespan 全部靠字符串动态 import。
 * - python_multipart / multipart：FastAPI 的 UploadFile 依赖，按需 import 容易漏。
 * - starlette / fastapi：子模块 + 少量模板/静态资源。
 */
const COLLECT_ALL = [
  'pypdf',
  'docx',
  'pptx',
  'openpyxl',
  'lxml',
  'cryptography',
  'sqlalchemy',
  'uvicorn',
  'python_multipart',
  'multipart',
  'starlette',
  'fastapi',
]

/** 单独补的 hidden import（collect-all 之外还漏的）。 */
const HIDDEN_IMPORTS = [
  'app.desktop_entry',
  'app.main',
  'sqlalchemy.dialects.sqlite',
  'sqlalchemy.dialects.sqlite.pysqlite',
  'uvicorn.logging',
  'uvicorn.loops.auto',
  'uvicorn.protocols.http.auto',
  'uvicorn.protocols.websockets.auto',
  'uvicorn.lifespan.on',
  'encodings.idna',
  'email.mime.text',
]

const t0 = Date.now()
const log = (...a) => console.log(`[build-backend] ${a.join(' ')}`)
const warn = (...a) => console.warn(`[build-backend] ${a.join(' ')}`)
const fail = (msg) => {
  console.error(`\n[build-backend] 失败：${msg}\n`)
  process.exit(1)
}
const elapsed = () => `${((Date.now() - t0) / 1000).toFixed(1)}s`
const mb = (p) => (statSync(p).size / 1024 / 1024).toFixed(1) + ' MB'

/** 不带 shell 地跑子进程（Windows 路径含空格时 shell:true 会二次解析，必须避免）。 */
function run(cmd, args, opts = {}) {
  return new Promise((res, rej) => {
    const child = spawn(cmd, args, {
      cwd: opts.cwd ?? ROOT,
      env: { ...process.env, ...(opts.env ?? {}) },
      stdio: opts.capture ? ['ignore', 'pipe', 'pipe'] : ['ignore', 'inherit', 'inherit'],
      windowsHide: true,
    })
    let out = ''
    let err = ''
    if (opts.capture) {
      child.stdout.on('data', (c) => { out += c })
      child.stderr.on('data', (c) => { err += c })
    }
    child.on('error', (e) => rej(new Error(`无法执行 ${cmd}：${e.message}`)))
    child.on('close', (code) => res({ code, out, err }))
  })
}

async function checkPython() {
  if (!existsSync(VENV_PYTHON)) {
    fail(`找不到项目虚拟环境 python：${VENV_PYTHON}\n` +
         `  请先创建 .venv 或通过 WRK_PYTHON 环境变量指定解释器。`)
  }
  const { code, out, err } = await run(VENV_PYTHON, ['-c', 'import sys; print(sys.version)'], { capture: true })
  if (code !== 0) fail(`venv python 不可用：${err}`)
  log(`python: ${out.trim().split('\n')[0]}`)
  log(`venv  : ${VENV_PYTHON}`)
}

/** 从锁文件读出固定的 PyInstaller 版本，脚本里不再写第二份版本号。 */
function pinnedPyInstallerVersion() {
  const text = readFileSync(BUILD_REQS, 'utf8')
  const m = text.match(/^\s*pyinstaller==([^\s#]+)/m)
  if (!m) fail(`锁文件里没有固定的 pyinstaller 版本：${BUILD_REQS}`)
  return m[1]
}

/** 确认 PyInstaller 不仅已安装，而且版本与锁文件一致；否则按锁文件重装。 */
async function ensurePyInstaller() {
  const want = pinnedPyInstallerVersion()
  const probe = await run(VENV_PYTHON, ['-c', 'import PyInstaller; print(PyInstaller.__version__)'], { capture: true })
  const got = probe.code === 0 ? probe.out.trim() : ''
  if (got === want) {
    log(`PyInstaller: ${want}（与锁文件一致）`)
    return
  }
  log(`PyInstaller 缺失或版本不符（期望 ${want}，实际 ${got || '无法 import'}），按锁文件安装 ...`)
  const inst = await run(VENV_PYTHON, ['-m', 'pip', 'install', '-r', BUILD_REQS, '-c', CONSTRAINTS])
  if (inst.code !== 0) fail('按锁文件安装构建依赖失败')
  const again = await run(VENV_PYTHON, ['-c', 'import PyInstaller; print(PyInstaller.__version__)'], { capture: true })
  const installed = again.code === 0 ? again.out.trim() : ''
  if (installed !== want) fail(`PyInstaller 版本应为 ${want}，实际 ${installed || '无法 import'}`)
  log(`PyInstaller: ${want}`)
}

/**
 * COLLECT_ALL 是固定的收集清单，不再按「当前 venv 装了什么」动态决定：
 * 依赖已由锁文件固定，任何缺失都说明环境漂移，必须立刻失败而不是静默跳过，
 * 否则两台机器打出的 exe 内容会不同。
 */
async function assertPackagesInstalled(names) {
  const code = [
    'import importlib.util as u, json',
    `names = ${JSON.stringify(names)}`,
    'print(json.dumps([n for n in names if u.find_spec(n) is not None]))',
  ].join('\n')
  const { code: rc, out, err } = await run(VENV_PYTHON, ['-c', code], { capture: true })
  if (rc !== 0) fail(`探测已安装包失败：${err}`)
  let installed
  try {
    installed = new Set(JSON.parse(out.trim().split('\n').pop()))
  } catch {
    fail(`探测已安装包输出无法解析：${out}`)
  }
  const missing = names.filter((n) => !installed.has(n))
  if (missing.length) {
    fail(`以下包缺失，环境不符合锁文件（请先按 README §3.1 安装依赖）：${missing.join(', ')}`)
  }
}

function writeLauncher() {
  mkdirSync(PYI_DIR, { recursive: true })
  writeFileSync(LAUNCHER, `# -*- coding: utf-8 -*-
"""PyInstaller 入口 launcher（由 frontend/scripts/build-backend.mjs 自动生成，勿手改）。

desktop_entry.py 使用相对导入（from .main import app），不能直接作为脚本打包，
这里以包内模块的方式导入后再调用 main()。
"""
import sys

from app.desktop_entry import main

if __name__ == "__main__":
    sys.exit(main())
`, 'utf8')
  log(`launcher: ${LAUNCHER}`)
}

async function build() {
  if (!existsSync(ENTRY)) fail(`找不到桌面入口：${ENTRY}`)

  await assertPackagesInstalled(COLLECT_ALL)

  const args = [
    '-m', 'PyInstaller',
    '--noconfirm',
    '--onefile',
    '--console',                       // 需要 stdout 输出 WRK_READY，不能用 --windowed
    '--name', 'wrk-backend',
    '--distpath', DIST_DIR,
    '--workpath', WORK_DIR,
    '--specpath', SPEC_DIR,
    '--paths', BACKEND_DIR,            // 让 `app` 包可被分析器找到
    '--noupx',                         // 不压壳：避免被杀软误报、也避免启动更慢
    '--log-level', 'WARN',
  ]
  if (!FAST) args.push('--clean')

  // 固定全量收集：清单是常量，不再根据当前 venv 动态增减。
  for (const p of COLLECT_ALL) args.push('--collect-all', p)
  for (const m of HIDDEN_IMPORTS) args.push('--hidden-import', m)

  args.push(LAUNCHER)

  log('PyInstaller 参数：')
  for (const a of args) console.log('    ' + a)

  rmSync(DIST_DIR, { recursive: true, force: true })
  mkdirSync(DIST_DIR, { recursive: true })

  log(`开始打包（${FAST ? '复用缓存' : '--clean 全量'}）...`)
  const { code } = await run(VENV_PYTHON, args, { cwd: BACKEND_DIR })
  if (code !== 0) fail(`PyInstaller 退出码 ${code}`)

  if (!existsSync(EXE)) fail(`打包完成但产物不存在：${EXE}`)
  log(`产物：${EXE}（${mb(EXE)}，用时 ${elapsed()}）`)
}

/**
 * 实测：拉起 exe → 等 WRK_READY → HTTP /api/health。
 * 关键：必须 WRK_WATCH_STDIN=0，否则父进程 stdin 一 EOF 后端就自我了断。
 */
async function verify() {
  log('--- 实测打包后的 exe ---')
  const dataDir = join(BUILD_DIR, '.verify-data')
  rmSync(dataDir, { recursive: true, force: true })
  mkdirSync(join(dataDir, 'db'), { recursive: true })
  mkdirSync(join(dataDir, 'files'), { recursive: true })
  mkdirSync(join(dataDir, 'secrets'), { recursive: true })

  const env = {
    ...process.env,
    WRK_DATA_DIR: dataDir,
    WRK_DB_PATH: join(dataDir, 'db', 'app.sqlite3'),
    WRK_SECRET_DIR: join(dataDir, 'secrets'),
    WRK_HOST: '127.0.0.1',
    WRK_PORT: '0',
    WRK_WATCH_STDIN: '0',        // 独立验证时不要让 stdin EOF 触发退出
    WRK_LOG_LEVEL: 'info',
    PYTHONIOENCODING: 'utf-8',
    PYTHONUNBUFFERED: '1',
  }

  const child = spawn(EXE, [], {
    cwd: DIST_DIR,
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  })

  const tail = []
  const keep = (line) => {
    tail.push(line)
    if (tail.length > 60) tail.shift()
  }
  let ready = null
  let readyResolve
  const readyPromise = new Promise((r) => { readyResolve = r })

  let buf = ''
  child.stdout.on('data', (chunk) => {
    buf += chunk.toString('utf8')
    const lines = buf.split(/\r?\n/)
    buf = lines.pop() ?? ''
    for (const line of lines) {
      keep('[out] ' + line)
      const m = line.match(/^WRK_READY (\{.*\})$/)
      if (m && !ready) {
        ready = JSON.parse(m[1])
        readyResolve(ready)
      }
    }
  })
  child.stderr.on('data', (chunk) => {
    for (const line of chunk.toString('utf8').split(/\r?\n/)) {
      if (line.trim()) keep('[err] ' + line)
    }
  })

  const exited = new Promise((r) => child.on('exit', (code, sig) => r({ code, sig })))

  const timeout = new Promise((_, rej) =>
    setTimeout(() => rej(new Error('等待 WRK_READY 超时（90s）')), 90000))

  try {
    await Promise.race([readyPromise, timeout, exited.then((e) => {
      throw new Error(`后端进程提前退出 code=${e.code} sig=${e.sig}`)
    })])
  } catch (e) {
    killTree(child)
    fail(`${e.message}\n--- 后端输出末尾 ---\n${tail.join('\n')}`)
  }

  log(`后端就绪：pid=${ready.pid} http://${ready.host}:${ready.port} v${ready.version}`)

  const url = `http://127.0.0.1:${ready.port}/api/health`
  let health = null
  let lastErr = ''
  for (let i = 0; i < 40; i++) {
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(3000) })
      const body = await res.json()
      if (res.ok && body.ok === true) { health = body; break }
      lastErr = `HTTP ${res.status} ${JSON.stringify(body)}`
    } catch (e) {
      lastErr = e.message
    }
    await new Promise((r) => setTimeout(r, 250))
  }

  if (!health) {
    killTree(child)
    fail(`/api/health 未通过：${lastErr}\n--- 后端输出末尾 ---\n${tail.join('\n')}`)
  }
  log(`/api/health → ${JSON.stringify(health)}`)

  // 顺带确认 /api/meta（走 DB + 初始化逻辑，能验证 sqlalchemy/sqlite 打进去了）
  try {
    const res = await fetch(`http://127.0.0.1:${ready.port}/api/meta`, { signal: AbortSignal.timeout(5000) })
    const meta = await res.json()
    log(`/api/meta → app=${meta.app_name} v${meta.version} initialized=${meta.initialized}`)
  } catch (e) {
    warn(`/api/meta 未通过（非致命）：${e.message}`)
  }

  // Windows + PyInstaller onefile：exe 是「引导器 + 真实进程」两层。
  // 只 kill 引导器会留下孤儿 Python 进程，它继续持有 stdout 管道 →
  // 父进程的 data 监听不解除 → 整个构建脚本永远不退出（实测卡死）。
  // 所以按进程树杀，并在结束后显式销毁管道。
  killTree(child)
  const killer = setTimeout(() => { killTree(child, true) }, 5000)
  const ex = await Promise.race([
    exited,
    new Promise((r) => setTimeout(() => r({ code: 'timeout', sig: null }), 8000)),
  ])
  clearTimeout(killer)
  child.stdout?.destroy()
  child.stderr?.destroy()
  log(`后端已退出 code=${ex.code}`)
  log(`--- 实测通过（${elapsed()}）---`)
}

/** 结束子进程（含 PyInstaller onefile 的真实子进程）。 */
function killTree(child, force = false) {
  try {
    if (process.platform === 'win32' && child.pid) {
      spawn('taskkill', ['/F', '/T', '/PID', String(child.pid)], {
        stdio: 'ignore', windowsHide: true,
      })
      return
    }
    child.kill(force ? 'SIGKILL' : 'SIGTERM')
  } catch {
    /* 进程已退出 */
  }
}

await checkPython()
await ensurePyInstaller()
writeLauncher()
await build()
if (NO_VERIFY) {
  log('已跳过实测（--no-verify）')
} else {
  await verify()
}

console.log('')
console.log('[build-backend] 完成')
console.log('  产物      : ' + EXE)
console.log('  体积      : ' + mb(EXE))
console.log('  总用时    : ' + elapsed())

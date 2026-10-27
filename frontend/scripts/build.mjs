#!/usr/bin/env node
/**
 * 一键构建：tsc -b && vite build → build-backend → electron-builder
 *
 * 用法：
 *   node scripts/build.mjs              # 生成 Windows NSIS 安装包
 *   node scripts/build.mjs --pack       # 只出 release/win-unpacked/（快速验证，不生成安装包）
 *   node scripts/build.mjs --skip-backend  # 复用已有 build/backend/wrk-backend.exe
 *   node scripts/build.mjs --skip-web      # 复用已有 dist/
 *
 * 环境变量：
 *   ELECTRON_BUILDER_CACHE  覆盖 electron-builder 的下载缓存目录（首次下载慢时可指向已有缓存）
 *   WRK_NODE               指定 node 可执行文件（默认 process.execPath）
 *
 * 设计要点（踩过的坑，改之前先看）：
 * 1. 本机全局有 ELECTRON_RUN_AS_NODE=1，会让 Electron 退化成纯 Node，
 *    electron-builder 探测 electron 版本会失败 → cleanEnv() 必须删掉它。
 * 2. electron-builder 的架构参数必须写 --x64，写 `--win x64` 会报 "Unknown target: x64"。
 * 3. 上一次构建残留的 wrk-backend.exe 进程会锁住 build/backend 目录，
 *    PyInstaller 清理时报 EPERM → 失败提示里要给出「先结束进程」的动作。
 */
import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const FRONTEND = resolve(HERE, '..')
const ROOT = resolve(FRONTEND, '..')
const NODE = process.env.WRK_NODE || process.execPath

const BACKEND_EXE = join(FRONTEND, 'build', 'backend', 'wrk-backend.exe')
const WEB_DIST = join(FRONTEND, 'dist')          // vite 产物
const OUT_DIR = join(FRONTEND, 'release')        // electron-builder 产物（见 electron-builder.yml 的 directories.output）
const UNPACKED = join(OUT_DIR, 'win-unpacked')
const ICON = join(FRONTEND, 'build', 'icon.ico')

const argv = process.argv.slice(2)
const PACK = argv.includes('--pack')
const SKIP_BACKEND = argv.includes('--skip-backend')
const SKIP_WEB = argv.includes('--skip-web')

const t0 = Date.now()
const log = (...a) => console.log(`[build] ${a.join(' ')}`)
const elapsed = () => `${((Date.now() - t0) / 1000).toFixed(1)}s`
const mb = (p) => (statSync(p).size / 1024 / 1024).toFixed(1) + ' MB'

/**
 * 关键：清掉 ELECTRON_RUN_AS_NODE。
 * 这台机器设了该变量，会让 Electron 退化为纯 Node 进程，
 * electron-builder 里跑 `electron --version` 之类的探测会失败/行为异常。
 */
function cleanEnv(extra = {}) {
  const env = { ...process.env, ...extra }
  delete env.ELECTRON_RUN_AS_NODE
  return env
}

/**
 * Windows 路径含空格时绝不能 shell:true，统一用 spawn 数组传参。
 *
 * 输出既转发到终端、也留一份在内存里：失败时要靠原始输出判断原因
 * （EPERM / 网络 / 缺模块），只 print 不捕获就没法翻译。
 * 缓冲只留尾部 64 KB，避免 electron-builder 的长日志吃内存。
 */
const OUTPUT_TAIL_LIMIT = 64 * 1024

function run(cmd, args, opts = {}) {
  return new Promise((res, rej) => {
    log(`$ ${cmd} ${args.map((a) => (a.includes(' ') ? JSON.stringify(a) : a)).join(' ')}`)
    const child = spawn(cmd, args, {
      cwd: opts.cwd ?? FRONTEND,
      env: cleanEnv(opts.env),
      stdio: ['inherit', 'pipe', 'pipe'],
      windowsHide: true,
    })
    let tail = ''
    const keep = (chunk) => {
      tail += chunk.toString('utf8')
      if (tail.length > OUTPUT_TAIL_LIMIT) tail = tail.slice(-OUTPUT_TAIL_LIMIT)
    }
    child.stdout.on('data', (c) => { process.stdout.write(c); keep(c) })
    child.stderr.on('data', (c) => { process.stderr.write(c); keep(c) })
    child.on('error', (e) => rej(new Error(`无法执行 ${cmd}：${e.message}`)))
    child.on('close', (code) => { run.lastOutput = tail; res(code) })
  })
}
run.lastOutput = ''

// ---------------------------------------------------------------- 失败原因翻译
//
// electron-builder / PyInstaller 的原始报错是英文 + 堆栈，对交付给用户的人没用。
// 这张表把最常见的失败翻译成「原因 + 怎么修」，原始输出仍完整打印在上方，
// 不丢信息，只是不再让用户自己读。
const FAILURE_HINTS = [
  {
    match: /EPERM|EBUSY|resource busy|being used by another process|另一个程序正在使用/i,
    title: '文件被占用，无法覆盖',
    fix: [
      '上一次构建的 wrk-backend.exe 或 企业知识库.exe 还在运行，锁住了输出目录。',
      '处理：结束这些进程后重跑本命令：',
      '  taskkill /F /IM wrk-backend.exe',
      '  taskkill /F /IM 企业知识库.exe',
      '或直接重启电脑（最省事）。',
    ],
  },
  {
    match: /ENOSPC|no space left|磁盘空间不足/i,
    title: '磁盘空间不足',
    fix: [
      '打包过程需要约 1 GB 临时空间（PyInstaller 中间件 + electron 解包 + 安装包）。',
      '处理：清理 release/、build/pyinstaller/work/ 或换到空间充足的磁盘。',
    ],
  },
  {
    match: /ETIMEDOUT|ENOTFOUND|ECONNREFUSED|getaddrinfo|request to .* failed|net::/i,
    title: '下载依赖失败（网络问题）',
    fix: [
      'electron-builder 首次运行需要下载 electron 二进制与 NSIS 工具链。',
      '处理：',
      '  1) 确认能访问 github.com 与 registry.npmjs.org；',
      '  2) 公司内网可设代理：set HTTPS_PROXY=http://proxy.corp:8080',
      '  3) 或把 ELECTRON_BUILDER_CACHE 指向已有缓存的目录复用。',
    ],
  },
  {
    match: /Cannot find module|MODULE_NOT_FOUND|ENOENT.*node_modules/i,
    title: '依赖未安装或已损坏',
    fix: [
      '处理：在 frontend/ 下重装依赖：',
      '  node "C:/Users/Administrator/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/pnpm/bin/pnpm.mjs" install',
      '若仍失败，删掉 node_modules 后重装。',
    ],
  },
  {
    match: /Unknown target|invalid target|Unsupported target/i,
    title: 'electron-builder 目标参数写错',
    fix: [
      '架构参数必须写 --x64；写成 `--win x64` 会被当成 target 名。',
      '处理：检查本脚本 builderArgs，不要手工改成 --win x64。',
    ],
  },
  {
    match: /code signing|signtool|signAndEditExecutable|certificate/i,
    title: '代码签名失败',
    fix: [
      '本项目无签名证书，electron-builder.yml 已设 forceCodeSigning: false。',
      '处理：确认 electron-builder.yml 里 forceCodeSigning 仍是 false、publish 仍是 null；',
      '若装了杀毒软件拦截签名工具，把 release/ 加入白名单。',
    ],
  },
  {
    match: /No such file or directory.*(icon|\.ico)|cannot find.*icon/i,
    title: '图标文件缺失或格式不对',
    fix: [
      '图标是可选的：build/icon.ico 存在就用，不存在回退 Electron 默认图标。',
      '处理：若确实要自定义图标，放一个 256x256 的 .ico 到 frontend/build/icon.ico。',
    ],
  },
  {
    match: /Python.*not found|找不到项目虚拟环境|venv/i,
    title: 'Python 虚拟环境不可用',
    fix: [
      '后端打包需要项目 .venv（内含 PyInstaller 与全部依赖）。',
      '处理：确认 F:\\GItHub_Code\\Wiki-Rag-Knowledge\\.venv\\Scripts\\python.exe 存在；',
      '或设 WRK_PYTHON 指向可用解释器。',
    ],
  },
]

/** 把失败输出翻成人话；匹配不到时给通用检查清单。 */
function explainBuildFailure(stage, code, output) {
  const text = String(output || '')
  for (const hint of FAILURE_HINTS) {
    if (hint.match.test(text)) {
      return { title: hint.title, fix: hint.fix }
    }
  }
  return {
    title: `「${stage}」失败（退出码 ${code}）`,
    fix: [
      '先看上面完整输出里第一条 Error / ERROR 行，通常是根因。',
      '常见排查顺序：',
      '  1) 是否有残留进程锁住文件（taskkill /F /IM wrk-backend.exe）；',
      '  2) node_modules 是否完整（pnpm install）；',
      '  3) 磁盘剩余空间是否 > 2 GB；',
      '  4) 网络能否下载 electron 二进制（首次构建需要）。',
    ],
  }
}

async function step(title, cmd, args, opts) {
  log(`--- ${title} ---`)
  const code = await run(cmd, args, opts)
  if (code !== 0) {
    const explained = explainBuildFailure(title, code, run.lastOutput)
    console.error('')
    console.error(`[build] ✗ ${explained.title}`)
    for (const line of explained.fix) console.error(`        ${line}`)
    console.error('')
    process.exit(code || 1)
  }
}

/** electron-builder CLI 路径（避免依赖 PATH 里的 .bin 垫片，Windows 上更稳）。 */
function builderCli() {
  const p = join(FRONTEND, 'node_modules', 'electron-builder', 'cli.js')
  if (!existsSync(p)) {
    console.error('[build] ✗ 找不到 electron-builder（node_modules 不完整）')
    console.error('        处理：在 frontend/ 下执行')
    console.error('          node "C:/Users/Administrator/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/pnpm/bin/pnpm.mjs" install')
    process.exit(1)
  }
  return p
}

// ---------- 0. 图标（可选） ----------
// electron-builder.yml 的 buildResources 指向 build/，icon.ico 存在就自动使用。
// 这里只做提示，不让缺图标阻断构建。
if (existsSync(ICON)) {
  log(`图标: ${ICON}（${mb(ICON)}）`)
} else {
  log('图标: 未提供 build/icon.ico，将使用 Electron 默认图标（不影响构建）')
}

// ---------- 1. 前端 ----------
if (SKIP_WEB) {
  log('跳过前端构建（--skip-web）')
} else {
  // tsc -b 与 vite build 分两步，失败时能立刻看出是哪一步
  await step('TypeScript 编译', NODE, [join(FRONTEND, 'node_modules', 'typescript', 'bin', 'tsc'), '-b'])
  await step('Vite 构建', NODE, [join(FRONTEND, 'node_modules', 'vite', 'bin', 'vite.js'), 'build'])
}

if (!existsSync(join(WEB_DIST, 'index.html'))) {
  console.error(`[build] ✗ 前端产物缺失：${join(WEB_DIST, 'index.html')}`)
  console.error('        处理：不要带 --skip-web，让脚本先跑 vite build。')
  process.exit(1)
}
log('前端产物 OK: dist/index.html')

// ---------- 2. 后端 sidecar ----------
if (SKIP_BACKEND) {
  if (!existsSync(BACKEND_EXE)) {
    console.error(`[build] ✗ --skip-backend 但产物不存在：${BACKEND_EXE}`)
    console.error('        处理：去掉 --skip-backend 重新构建后端，或先单独跑：')
    console.error('          node scripts/build-backend.mjs')
    process.exit(1)
  }
  log(`跳过后端打包（--skip-backend），复用 ${BACKEND_EXE}（${mb(BACKEND_EXE)}）`)
} else {
  await step('PyInstaller 打包后端', NODE, [join(HERE, 'build-backend.mjs')])
  if (!existsSync(BACKEND_EXE)) {
    console.error(`[build] ✗ 后端产物缺失：${BACKEND_EXE}`)
    console.error('        处理：单独跑 `node scripts/build-backend.mjs` 看完整报错。')
    process.exit(1)
  }
  log(`后端产物 OK: ${BACKEND_EXE}（${mb(BACKEND_EXE)}）`)
}

// ---------- 3. electron-builder ----------
mkdirSync(OUT_DIR, { recursive: true })

// electron-builder 的架构参数必须写成 --x64，不能写 `--win x64`：
// 后者会把 x64 当成 target 名，报 "Unknown target: x64"。
const builderArgs = ['--win', '--x64', '--config', join(FRONTEND, 'electron-builder.yml')]
if (PACK) builderArgs.push('--dir')
else builderArgs.push('--publish', 'never')

await step(
  PACK ? 'electron-builder 打包目录（--dir）' : 'electron-builder 生成 NSIS 安装包',
  NODE,
  [builderCli(), ...builderArgs],
  // 首次会下载 electron 二进制 + nsis 工具链，给足时间
  { cwd: FRONTEND },
)

// ---------- 4. 结果核对 ----------
console.log('')
log('--- 产物核对 ---')

let ok = true
const check = (label, p) => {
  if (existsSync(p)) log(`  ✓ ${label}: ${p}`)
  else { console.error(`  ✗ ${label} 缺失: ${p}`); ok = false }
}

if (existsSync(UNPACKED)) {
  check('应用主程序', join(UNPACKED, '企业知识库.exe'))
  const sidecar = join(UNPACKED, 'resources', 'backend', 'wrk-backend.exe')
  check('后端 sidecar', sidecar)
  if (existsSync(sidecar)) log(`    sidecar 体积: ${mb(sidecar)}`)
  check('asar', join(UNPACKED, 'resources', 'app.asar'))
} else if (PACK) {
  console.error(`  ✗ --pack 模式未产出 ${UNPACKED}`)
  console.error('        处理：看上面 electron-builder 的输出，通常是文件被占用或缺少 electron 二进制。')
  ok = false
} else {
  // NSIS 模式：安装包在 release/ 根下
  const installers = readdirSync(OUT_DIR).filter((f) => f.endsWith('.exe'))
  if (installers.length === 0) {
    console.error('  ✗ 未找到 NSIS 安装包')
    console.error('        处理：确认 electron-builder 已下载 nsis 工具链（首次构建需要联网）。')
    ok = false
  } else {
    for (const f of installers) {
      const p = join(OUT_DIR, f)
      log(`  ✓ 安装包: ${p}（${mb(p)}）`)
    }
  }
}

console.log('')
if (!ok) {
  console.error(`[build] ✗ 有产物缺失（用时 ${elapsed()}）`)
  process.exit(1)
}
log(`完成（用时 ${elapsed()}）`)
if (PACK) log(`可直接运行：${join(UNPACKED, '企业知识库.exe')}`)

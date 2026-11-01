// Electron 主进程：管理 Python 后端 sidecar 的生命周期，加载前端界面。
//
// 关键设计：
// 1. 后端是子进程，端口动态分配，就绪后通过 stdout 的 WRK_READY 行回报；
// 2. 渲染进程只加载本地打包的静态资源，不开放远程内容；
// 3. 数据目录放在用户 AppData，卸载重装不丢数据；
// 4. 后端异常退出时给出可操作的提示，而不是白屏。
const { app, BrowserWindow, dialog, ipcMain, shell, Menu } = require('electron')
const { spawn, spawnSync } = require('node:child_process')
const { existsSync, mkdirSync, appendFileSync } = require('node:fs')
const { join, dirname } = require('node:path')
const updater = require('./updater.cjs')

const HERE = __dirname
const ROOT = join(HERE, '..')
const DIST_DIR = join(ROOT, 'dist')
const isDev = process.env.WRK_DEV === '1'

// ---------------------------------------------------------------- 单实例
//
// 为什么必须在 whenReady 之前抢锁：每个实例都会拉起自己的 Python sidecar，
// 两个实例指向同一个 AppData 数据库时，后台 Worker 会各自轮询任务表，
// 同一个入库任务可能被领取两次（重复写分块、重复计费），用户侧表现为
// 「文档莫名其妙多了一份」或「任务卡住」。
// 第二个实例直接退出，把已有窗口叫到前台即可。
const gotSingleInstanceLock = app.requestSingleInstanceLock()
if (!gotSingleInstanceLock) {
  app.quit()
}

let backend = null
let backendInfo = null
let mainWindow = null
let quitting = false
let startupError = ''

// 主动停止后端期间不要弹「后端已停止」的错误框：重启后端走的就是这条路。
let stopping = false
// 停止流程的幂等令牌。before-quit / window-all-closed / process.on('exit')
// 会各自调一次 stopBackend()，这里合并成同一个 Promise，避免重复强杀。
let stopPromise = null
// 复用了已有后端（WRK_ATTACH）时不能去杀它 —— 那是别人拉起的进程。
let backendAttached = false
// before-quit 只拦一次，第二次直接放行。
let quitHandled = false

// 后端停止的时间预算（毫秒）：
// 先靠 stdin EOF 让它自己优雅退出；超过 STOP_GRACE_MS 还没死，基本可以认定
// 它卡住了（最常见是等 SQLite 写锁），这时必须强杀整棵进程树。
const STOP_GRACE_MS = 1500
const TASKKILL_TIMEOUT_MS = 8000

const logLines = []

// ---------------------------------------------------------------- 启动失败翻译
//
// 用户不该看到「退出码 1」。这张表把后端启动失败的原始信息翻译成
// 「发生了什么 + 下一步做什么」，原始串只保留在 detail 里给技术支持看。
// 关键字按顺序匹配，先命中先返回（具体原因在前，通用兜底在后）。
const FAILURE_HINTS = [
  {
    id: 'no-permission',
    title: '数据目录没有写入权限',
    action: '请确认当前 Windows 账号对数据目录有写权限；公司电脑可请管理员放开该目录权限后重试。',
    match: /permission|eacces|eperm|拒绝访问|read-only/i,
  },
  {
    id: 'port-in-use',
    title: '端口被其他程序占用了',
    action: '关掉占用端口的程序后点重试。桌面版通常会自己换端口，重试一般就能起来。',
    match: /eaddrinuse|address already in use|10048|端口/i,
  },
  {
    id: 'missing-runtime',
    title: '缺少运行库，后端无法启动',
    action: '通常是缺少 Microsoft Visual C++ 2015-2022 x64 运行库，安装后重启应用。',
    match: /vcruntime|msvcp|0xc000007b|dll|could not be loaded/i,
  },
  {
    id: 'missing-file',
    title: '后端程序文件缺失或被安全软件拦截',
    action: '安装可能不完整，或安全软件隔离了后端程序。请重新安装；装了杀毒软件的话把安装目录加白名单。',
    match: /enoent|no such file|not found|找不到|拦截|quarantin/i,
  },
  {
    id: 'timeout',
    title: '后端启动超时',
    action: '首次启动要初始化数据库，可能较慢。请点重试；连续失败请打开日志看卡在哪一步。',
    match: /60 秒内未就绪|超时|timeout|timed out/i,
  },
  {
    id: 'exited',
    title: '后端启动后立即退出',
    action: '数据库可能损坏或上次异常退出留了锁。先点重启后端；仍失败请打开日志发给技术支持。',
    match: /退出码|exit code|signal|信号/i,
  },
]

const FALLBACK_HINT = {
  id: 'unknown',
  title: '后端服务没能启动',
  action: '请先点重试。如果一直失败，点「查看详情」复制日志发给技术支持。',
}

/**
 * 把原始错误串翻译成人话。
 * @param {string} raw 主进程拼出的原始错误（可能含退出码、命令行、日志尾巴）
 */
function explainFailure(raw) {
  const text = String(raw || '')
  for (const hint of FAILURE_HINTS) {
    if (hint.match.test(text)) {
      return { id: hint.id, title: hint.title, action: hint.action, detail: text }
    }
  }
  return { ...FALLBACK_HINT, detail: text }
}

/** 最近一次启动失败的人话版本，供渲染进程查询。 */
let lastFailure = null

function failBackend(raw) {
  const explained = explainFailure(raw)
  lastFailure = explained
  log('启动失败（已翻译）：', `${explained.id} / ${explained.title}`)
  const err = new Error(explained.action)
  err.userTitle = explained.title
  err.userAction = explained.action
  err.detail = explained.detail
  err.reasonId = explained.id
  return err
}

// 启动日志同时写文件：打包后没有控制台，出问题时用户能拿到日志。
// 文件在 userData/logs/main.log，菜单「文件 → 打开日志」可直接打开。
let logFile = null
try {
  logFile = join(app.getPath('userData'), 'logs', 'main.log')
  mkdirSync(dirname(logFile), { recursive: true })
} catch {
  logFile = null
}

function log(...args) {
  const line = `[${new Date().toISOString()}] ${args.join(' ')}`
  logLines.push(line)
  if (logLines.length > 500) logLines.shift()
  if (isDev) console.log(line)
  if (logFile) {
    try {
      appendFileSync(logFile, line + '\n', 'utf8')
    } catch {
      /* 日志写失败不影响主流程 */
    }
  }
}

function userDataPaths() {
  const base = app.getPath('userData')
  return {
    base,
    data: join(base, 'data'),
    files: join(base, 'data', 'files'),
    db: join(base, 'data', 'db'),
    secrets: join(base, 'data', 'secrets'),
  }
}

function resolveBackendCommand(dataDir) {
  // 打包后：resources/backend/wrk-backend(.exe)
  // 开发时：直接用 venv 里的 python 跑模块
  //
  // 两种方式都显式传 --data-dir：进程命令行里带上本应用的数据目录，
  // killStaleBackends() 才能把「我们的后端」和用户机器上其他 Python 区分开。
  const dataArgs = dataDir ? ['--data-dir', dataDir] : []
  const packaged = process.platform === 'win32'
    ? join(process.resourcesPath, 'backend', 'wrk-backend.exe')
    : join(process.resourcesPath, 'backend', 'wrk-backend')

  if (existsSync(packaged)) {
    return { cmd: packaged, args: dataArgs, cwd: dirname(packaged), packaged: true }
  }

  const venvPython = process.platform === 'win32'
    ? join(ROOT, '..', '.venv', 'Scripts', 'python.exe')
    : join(ROOT, '..', '.venv', 'bin', 'python')
  const python = process.env.WRK_PYTHON || (existsSync(venvPython) ? venvPython : 'python')
  const backendDir = join(ROOT, '..', 'backend')
  return {
    cmd: python,
    args: ['-m', 'app.desktop_entry', ...dataArgs],
    cwd: backendDir,
    packaged: false,
  }
}

async function startBackend() {
  const paths = userDataPaths()
  for (const p of [paths.data, paths.files, paths.db, paths.secrets]) {
    mkdirSync(p, { recursive: true })
  }

  // 拉起新后端之前先把上次没退干净的残留收掉：它们还占着同一个数据目录、
  // 同一个 SQLite 文件，不清掉的话新实例一起来就是 database is locked。
  await killStaleBackends('启动前清理')

  return new Promise((resolve, reject) => {
    const { cmd, args, cwd, packaged } = resolveBackendCommand(paths.data)
    log('启动后端：', cmd, args.join(' '))

    const env = {
      ...process.env,
      PYTHONIOENCODING: 'utf-8',
      PYTHONUNBUFFERED: '1',
      WRK_DATA_DIR: paths.data,
      WRK_DB_PATH: join(paths.db, 'app.sqlite3'),
      WRK_SECRET_DIR: paths.secrets,
      WRK_HOST: '127.0.0.1',
      WRK_PORT: process.env.WRK_PORT || '0',
      WRK_WORKER_ENABLED: '1',
      WRK_WATCH_STDIN: '1',
      WRK_HTTP_TRUST_ENV: '0',
      // 父进程守护要盯的是 Electron 自己，不是 PyInstaller 的 bootloader：
      // bootloader 自己握着一份 stdin 句柄，父进程死了它拿不到 EOF。
      WRK_PARENT_PID: String(process.pid),
    }
    if (packaged) {
      // 打包后是单文件，模块已在可执行文件内
      env.WRK_PACKAGED = '1'
    } else {
      env.PYTHONPATH = cwd
    }

    backendAttached = false
    backend = spawn(cmd, args, {
      cwd, env, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
      // POSIX 下让后端单独成组，停止时用负 pid 一次收掉整棵树；
      // Windows 不需要（那边用 taskkill /PID <pid> /T）。
      detached: process.platform !== 'win32',
    })

    let settled = false
    let buffer = ''

    const onData = (chunk) => {
      buffer += chunk.toString('utf8')
      const lines = buffer.split(/\r?\n/)
      buffer = lines.pop() ?? ''
      for (const line of lines) {
        log('backend:', line)
        const ready = line.match(/^WRK_READY (\{.*\})$/)
        // 数据目录锁发现已有实例还活着时，后端会打这一行然后自己退出。
        // 直接复用那个实例：它已经在跑 Worker，不需要第二个。
        const attach = line.match(/^WRK_ATTACH (\{.*\})$/)
        const m = ready || attach
        if (m && !settled) {
          settled = true
          try {
            backendInfo = JSON.parse(m[1])
            backendAttached = Boolean(attach)
            if (backendAttached) log('复用已在运行的后端：', JSON.stringify(backendInfo))
            resolve(backendInfo)
          } catch (e) {
            reject(new Error(`后端就绪信息解析失败：${e}`))
          }
        }
        if (/首次部署：已创建管理员账号/.test(line)) {
          // 把初始密码记录到日志文件，同时弹窗提示用户查看
          const pwdLine = lines.find((l) => l.includes('密码：'))
          if (pwdLine) {
            startupError = pwdLine.trim()
            log('初始管理员信息：', startupError)
          }
        }
      }
    }

    backend.stdout.on('data', onData)
    backend.stderr.on('data', (chunk) => {
      const text = chunk.toString('utf8')
      log('backend-err:', text.trim().slice(0, 500))
      if (!settled) {
        const m = text.match(/WRK_READY (\{.*\})/)
        if (m) {
          settled = true
          backendInfo = JSON.parse(m[1])
          resolve(backendInfo)
        }
      }
    })

    backend.on('error', (err) => {
      log('后端进程启动失败：', err.message)
      if (!settled) {
        settled = true
        reject(failBackend(
          `无法启动后端服务：${err.message}\n\n命令：${cmd} ${args.join(' ')}`))
      }
    })

    backend.on('exit', (code, signal) => {
      log(`后端退出 code=${code} signal=${signal}`)
      // 复用的那个后端不是我们拉起的（它自己打印完 WRK_ATTACH 就退了），
      // 退出与否都不该弹框，更不该去杀它。
      if (backendAttached) return
      if (!settled) {
        const finish = () => {
          if (settled) return
          settled = true
          reject(failBackend(
            `后端服务启动失败（退出码 ${code}）。\n\n` +
            `命令：${cmd} ${args.join(' ')}\n工作目录：${cwd}\n\n` +
            `最近日志：\n${logLines.slice(-15).join('\n')}`))
        }
        // 退出码 0 时先等一拍：attach 场景下 stdout 的 WRK_ATTACH 可能
        // 比 exit 事件晚到一点，不能把它误判成「启动后立即退出」。
        if (code === 0) setTimeout(finish, 200)
        else finish()
        return
      }
      if (!quitting && !stopping) {
        const explained = explainFailure(`后端服务已停止（退出码 ${code}）`)
        dialog.showErrorBox(
          explained.title,
          `${explained.action}\n\n技术细节：退出码 ${code} signal ${signal || '-'}\n` +
          `日志位置：${logFile || '（不可用）'}`)
      }
    })

    setTimeout(() => {
      if (!settled) {
        settled = true
        reject(failBackend(`后端服务在 60 秒内未就绪。\n\n命令：${cmd} ${args.join(' ')}\n\n` +
          `最近日志：\n${logLines.slice(-15).join('\n')}`))
      }
    }, 60000)
  })
}

// ---------------------------------------------------------------- 后端生命周期

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/** 进程是否还在。Node 在 Windows 上也是用 OpenProcess 探活，不会真发信号。 */
function pidAlive(pid) {
  if (!pid) return false
  try {
    process.kill(pid, 0)
    return true
  } catch (e) {
    // EPERM = 进程还在但没权限；ESRCH = 已经退出了
    return Boolean(e && e.code === 'EPERM')
  }
}

/**
 * 强杀整棵进程树（同步）。退出路径上只能同步，所以用 spawnSync。
 *
 * 为什么必须 /T：打包后的后端是 PyInstaller 单文件程序，Electron spawn 到的
 * 只是 bootloader，真正的 Python 进程是它的子进程。只杀 bootloader 的话，
 * 真进程会被系统过继成孤儿，继续跑 Worker、占着 SQLite 写锁 —— 这正是
 * 「应用关了但任务还在跑、重开 database is locked」的根因。
 */
function hardKillTree(pid) {
  if (!pid) return { ok: false }
  if (process.platform === 'win32') {
    const r = spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], {
      timeout: TASKKILL_TIMEOUT_MS, windowsHide: true, encoding: 'utf8',
    })
    const ok = r.status === 0
    log(`taskkill /PID ${pid} /T /F -> status=${r.status}`,
        String(r.error || r.stdout || r.stderr || '').trim().slice(0, 300))
    return { ok, status: r.status }
  }
  // POSIX：spawn 时用了 detached，pid 就是进程组号，负号一次收掉整组
  for (const target of [-pid, pid]) {
    try {
      process.kill(target, 'SIGKILL')
      return { ok: true }
    } catch { /* 试下一个 */ }
  }
  return { ok: false }
}

/**
 * 列出可能属于本应用的进程（仅 Windows）。失败只记日志，不影响启动。
 *
 * 用 Get-CimInstance 而不是 tasklist：只有它同时给出 ExecutablePath 和
 * CommandLine，能按「可执行文件路径 + 数据目录参数」精确锁定残留后端。
 */
function listCandidateProcesses() {
  if (process.platform !== 'win32') return []
  const script = [
    // 不设输出编码的话，中文用户名路径会按 OEM 码页写出乱码，路径比对就废了
    '[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;',
    'Get-CimInstance Win32_Process | Where-Object {',
    "@('wrk-backend.exe','python.exe','pythonw.exe') -contains $_.Name }",
    '| Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine',
    '| ConvertTo-Json -Compress',
  ].join(' ')
  const r = spawnSync('powershell.exe', [
    '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script,
  ], { timeout: 15000, windowsHide: true, encoding: 'utf8' })
  if (r.error || r.status !== 0) {
    log('枚举残留进程失败（忽略）：', String(r.error || r.stderr || '').slice(0, 300))
    return []
  }
  const text = String(r.stdout || '').trim()
  if (!text) return []
  try {
    const parsed = JSON.parse(text)
    return Array.isArray(parsed) ? parsed : [parsed]
  } catch (e) {
    log('解析进程列表失败（忽略）：', String(e))
    return []
  }
}

/**
 * 判断某个进程是不是「本应用的后端」。
 *
 * 宁可漏杀也不误杀用户机器上的其他 Python：
 *   1. 可执行文件路径和本安装目录下的 wrk-backend.exe 完全一致 → 一定是；
 *   2. 或命令行里带 app.desktop_entry（开发模式走 venv python）→ 一定是
 *      （这个模块名只可能来自本仓库）；
 *   3. 数据目录参数命中时再额外确认一次（正常情况第 1/2 条已经够了）。
 */
function isOurBackend(proc, ctx) {
  const pid = Number(proc.ProcessId || 0)
  if (!pid || pid === process.pid) return false
  const exe = String(proc.ExecutablePath || '').toLowerCase()
  const cmd = String(proc.CommandLine || '').toLowerCase()
  // 路径里有空格时命令行会带引号，比对前先去掉
  const flat = cmd.replace(/["']/g, '')
  if (ctx.exeLower && exe === ctx.exeLower) return true
  if (ctx.dataLower && flat.includes(ctx.dataLower) &&
      (exe.endsWith('wrk-backend.exe') || exe.endsWith('wrk-backend'))) {
    return true
  }
  return exe.includes('python') && cmd.includes('app.desktop_entry')
}

/**
 * 启动前 / 退出时清理上次没退干净的残留后端。
 *
 * 场景：上次 Electron 被任务管理器或崩溃干掉，PyInstaller 的 bootloader 与
 * 真进程都活了下来。它们还占着同一个数据目录、同一个 SQLite 文件，新实例
 * 一起来就是 database is locked。必须在拉起新后端之前按 pid 收掉。
 *
 * 清理失败只记日志、绝不阻塞启动：后端那边的数据目录锁 + 父进程守护会兜底。
 */
async function killStaleBackends(reason = '启动前清理') {
  if (process.platform !== 'win32') return 0
  const paths = userDataPaths()
  const exePath = join(process.resourcesPath, 'backend', 'wrk-backend.exe')
  const ctx = {
    exeLower: existsSync(exePath) ? exePath.toLowerCase() : '',
    dataLower: paths.data.replace(/["']/g, '').toLowerCase(),
  }
  const candidates = listCandidateProcesses()
  const targets = candidates.filter((p) => isOurBackend(p, ctx))
  if (!targets.length) {
    log(`${reason}：没有发现残留后端进程`)
    return 0
  }
  let killed = 0
  for (const proc of targets) {
    const pid = Number(proc.ProcessId)
    if (!pidAlive(pid)) continue
    log(`${reason}：发现残留后端 pid=${pid} 父=${proc.ParentProcessId} exe=${proc.ExecutablePath || '?'}`)
    const r = hardKillTree(pid)
    if (r.ok) killed += 1
    else log(`${reason}：强杀 pid=${pid} 失败（忽略，继续启动）`)
  }
  log(`${reason}：清理残留后端 ${killed}/${targets.length}`)
  return killed
}

/**
 * 停止后端（幂等）。
 *
 * 顺序：stdin EOF（让后端自己收尾）→ 等 STOP_GRACE_MS → 还活着就强杀进程树。
 *
 * 为什么不再用 backend.killed 判断：child.kill() 一返回 killed 就是 true，
 * 之前的 SIGKILL 兜底因此永远不执行 —— 而真正需要兜底的恰恰是
 * 「kill 了但进程没死」这种情况（bootloader 死了、真进程变孤儿）。
 */
function stopBackend() {
  if (stopPromise) return stopPromise
  const child = backend
  if (!child) return Promise.resolve()

  if (backendAttached) {
    // 复用别人拉起的后端：不杀、不等，直接放手
    log('后端是复用实例，退出时不停止它')
    backend = null
    backendInfo = null
    return Promise.resolve()
  }

  const pid = child.pid
  stopping = true
  stopPromise = (async () => {
    log('停止后端 pid=', pid)
    try { child.stdin?.end() } catch { /* 管道可能已经关了 */ }

    const deadline = Date.now() + STOP_GRACE_MS
    while (Date.now() < deadline) {
      if (!pidAlive(pid)) {
        log('后端已自行退出 pid=', pid)
        return
      }
      await sleep(100)
    }

    log(`后端 ${STOP_GRACE_MS}ms 内未退出，强杀进程树 pid=`, pid)
    hardKillTree(pid)
    // 再扫一遍：万一 bootloader 先死、真进程已经被过继出去，taskkill /T
    // 找不到子进程，就靠按可执行文件路径 + 数据目录参数把残留收掉。
    await killStaleBackends('退出兜底')
  })().catch((e) => {
    log('停止后端出错：', String(e))
  }).finally(() => {
    backend = null
    backendInfo = null
    stopPromise = null
    stopping = false
  })
  return stopPromise
}

/** 同步版停止：只能用在 process.on('exit') 这种不允许 await 的地方。 */
function stopBackendSync() {
  const child = backend
  backend = null
  backendInfo = null
  if (!child || !child.pid || backendAttached) return
  log('同步停止后端 pid=', child.pid)
  try { child.stdin?.end() } catch { /* 忽略 */ }
  if (pidAlive(child.pid)) hardKillTree(child.pid)
}

/** 开发模式允许的源：dev server 地址（同源子路径也算）。 */
function isDevUrl(url) {
  const base = process.env.WRK_DEV_URL || 'http://localhost:5173'
  try {
    const target = new URL(url)
    const allowed = new URL(base)
    return target.origin === allowed.origin
  } catch {
    return false
  }
}

/**
 * 打包后允许的导航目标：只认 dist 目录下的文件。
 * file URL 的 pathname 是 `/F:/...` 形式，Windows 上要先去掉前导斜杠再比较。
 */
function isAppFileUrl(url) {
  try {
    const u = new URL(url)
    if (u.protocol !== 'file:') return false
    const raw = decodeURIComponent(u.pathname)
    const filePath = /^\/[A-Za-z]:/.test(raw) ? raw.slice(1) : raw
    const root = DIST_DIR.replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase()
    return filePath.replace(/\\/g, '/').toLowerCase().startsWith(`${root}/`)
  } catch {
    return false
  }
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 1100,
    minHeight: 720,
    show: false,
    backgroundColor: '#f5f7fa',
    title: '企业知识库 · 文档整理与智能问答',
    webPreferences: {
      preload: join(HERE, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      webSecurity: true,
    },
  })

  mainWindow.once('ready-to-show', () => mainWindow.show())

  if (isDev) {
    mainWindow.loadURL(process.env.WRK_DEV_URL || 'http://localhost:5173')
  } else {
    mainWindow.loadFile(join(ROOT, 'dist', 'index.html'))
  }

  // 外部链接用系统浏览器打开，不在应用内导航
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/.test(url)) shell.openExternal(url)
    return { action: 'deny' }
  })
  // 只允许停留在自己的界面里：开发时是 dev server，打包后是 dist 目录。
  // 之前只要 file:// 就放行，页面里一个手滑的 <a href="file:///C:/...">
  // 就能把应用窗口导航到任意本地文件。
  mainWindow.webContents.on('will-navigate', (event, url) => {
    const ok = isDev ? isDevUrl(url) : isAppFileUrl(url)
    if (!ok) {
      event.preventDefault()
      if (/^https?:/.test(url)) shell.openExternal(url)
    }
  })

  mainWindow.on('closed', () => { mainWindow = null })
}

function buildMenu() {
  const template = [
    {
      label: '文件',
      submenu: [
        { label: '打开数据目录', click: () => shell.openPath(userDataPaths().base) },
        { label: '打开日志', click: () => shell.openPath(app.getPath('logs')) },
        { type: 'separator' },
        { role: 'quit', label: '退出' },
      ],
    },
    {
      label: '编辑',
      submenu: [
        { role: 'undo', label: '撤销' }, { role: 'redo', label: '重做' },
        { type: 'separator' },
        { role: 'cut', label: '剪切' }, { role: 'copy', label: '复制' },
        { role: 'paste', label: '粘贴' }, { role: 'selectAll', label: '全选' },
      ],
    },
    {
      label: '视图',
      submenu: [
        { role: 'reload', label: '重新加载' },
        { role: 'toggleDevTools', label: '开发者工具' },
        { type: 'separator' },
        { role: 'resetZoom', label: '实际大小' },
        { role: 'zoomIn', label: '放大' }, { role: 'zoomOut', label: '缩小' },
        { type: 'separator' },
        { role: 'togglefullscreen', label: '全屏' },
      ],
    },
    {
      label: '帮助',
      submenu: [
        {
          label: '检查更新',
          click: () => void updater.check(true),
        },
        { type: 'separator' },
        {
          label: '关于',
          click: () => dialog.showMessageBox({
            type: 'info', title: '关于',
            message: '企业知识库 · 文档整理与智能问答系统',
            detail: `版本 ${app.getVersion()}\n后端 ${backendInfo ? backendInfo.version : '未启动'}\n` +
                    `服务地址 http://127.0.0.1:${backendInfo?.port ?? '-'}\n` +
                    `数据目录 ${userDataPaths().base}`,
          }),
        },
      ],
    },
  ]
  Menu.setApplicationMenu(Menu.buildFromTemplate(template))
}

// 渲染进程通过 preload 暴露的接口拿后端地址与运行信息
ipcMain.handle('wrk:info', () => ({
  apiBase: backendInfo ? `http://127.0.0.1:${backendInfo.port}` : '',
  version: app.getVersion(),
  backendVersion: backendInfo?.version ?? '',
  // WRK_READY 里本来就有后端 pid，之前没转发，关于页只能从日志里正则扒。
  // 转发真实值，前端按可选字段读（拿不到就回退到日志解析）。
  backendPid: backendInfo?.pid ?? null,
  dataDir: userDataPaths().base,
  // 关于页「安装位置」与各层版本号用
  installDir: app.getAppPath(),
  installDirExe: process.execPath,
  logDir: app.getPath('logs'),
  electronVersion: process.versions.electron,
  chromeVersion: process.versions.chrome,
  nodeVersion: process.versions.node,
  platform: `${process.platform} ${process.arch}`,
  isPackaged: app.isPackaged,
  isDev,
}))

ipcMain.handle('wrk:logs', () => logLines.slice(-200).join('\n'))

/**
 * 启动失败的完整说明：人话 + 技术细节分开给，前端把 detail 折叠进「查看详情」。
 * 桌面版启动失败时窗口可能还没建出来，所以渲染进程也要能拿到这份数据。
 */
ipcMain.handle('wrk:failure', () => lastFailure)

ipcMain.handle('wrk:openPath', (_e, kind) => {
  const p = kind === 'data'
    ? userDataPaths().base
    : kind === 'install'
      ? (app.isPackaged ? dirname(process.execPath) : app.getAppPath())
      : app.getPath('logs')
  return shell.openPath(p)
})

ipcMain.handle('wrk:restartBackend', async () => {
  // 必须等后端真正死掉再拉起：否则新旧两个后端会同时持有数据目录
  await stopBackend()
  await new Promise((r) => setTimeout(r, 300))
  try {
    await startBackend()
    lastFailure = null
    return { ok: true, port: backendInfo?.port }
  } catch (e) {
    return {
      ok: false,
      // 人话版本给界面，原始串留在 detail
      error: e.userAction || String(e),
      title: e.userTitle || '后端服务没能启动',
      detail: e.detail || String(e),
      reasonId: e.reasonId || 'unknown',
    }
  }
})

app.whenReady().then(async () => {
  // 没抢到锁的实例已经 quit()，这里直接返回，避免再拉起一个 sidecar。
  if (!gotSingleInstanceLock) return
  log('=== 应用启动 ===', `electron=${process.versions.electron}`,
      `dev=${isDev}`, `userData=${app.getPath('userData')}`)
  buildMenu()
  try {
    await startBackend()
    log('后端就绪：', JSON.stringify(backendInfo))
    createWindow()
    log('窗口已创建')
    // 更新检查放在窗口创建之后：状态变化要推给渲染进程，且失败不能影响启动
    updater.init({ log })
    if (startupError) {
      dialog.showMessageBox(mainWindow, {
        type: 'info', title: '首次部署',
        message: '已创建管理员账号',
        detail: `${startupError}\n\n首次登录后请立即修改密码。\n\n` +
                `该信息同时记录在日志中，可通过「文件 → 打开日志」查看。`,
      })
    }
  } catch (e) {
    log('启动失败：', String(e))
    // 主文案给人话，技术细节放 detail —— 用户不需要先读懂退出码
    dialog.showMessageBox({
      type: 'error',
      title: e.userTitle || '应用启动失败',
      message: e.userTitle || '应用启动失败',
      detail: `${e.userAction || '请重试；仍然失败请把日志发给技术支持。'}\n\n` +
              `技术细节：\n${(e.detail || String(e)).slice(0, 1200)}\n\n` +
              `日志位置：${logFile || '（不可用）'}`,
      buttons: ['退出'],
    }).finally(() => app.quit())
  }
})

app.on('second-instance', () => {
  // 用户又双击了一次图标：把已有窗口叫到前台，不新建实例。
  if (!mainWindow) {
    if (backendInfo) createWindow()
    return
  }
  if (mainWindow.isMinimized()) mainWindow.restore()
  mainWindow.show()
  mainWindow.focus()
})

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0 && backendInfo) createWindow()
})

app.on('before-quit', (event) => {
  quitting = true
  if (quitHandled) return
  quitHandled = true
  // 第一次进入：先拦下退出，等后端真正死掉再退。
  // 不拦的话 Electron 会立刻结束主进程，stdin EOF 还没让后端收尾就只剩
  // taskkill 一条路 —— 也就是原来留孤儿的那条路。
  event.preventDefault()
  let finished = false
  const finish = () => {
    if (finished) return
    finished = true
    updater.dispose()
    app.exit(0)
  }
  stopBackend().finally(finish)
  // 再兜一层：停止流程自己卡住也不能让应用退不掉
  setTimeout(finish, STOP_GRACE_MS + TASKKILL_TIMEOUT_MS + 2000)
})

app.on('window-all-closed', () => {
  quitting = true
  void stopBackend()
  if (process.platform !== 'darwin') app.quit()
})

// 最后一道保险：进程无论怎么退，都不许把后端留在后台。
process.on('exit', () => stopBackendSync())

// 开发时在终端里 Ctrl+C：给后端一次同步收尾的机会，别留孤儿。
for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => {
    log('收到信号', sig)
    quitting = true
    stopBackendSync()
    app.exit(0)
  })
}

process.on('uncaughtException', (e) => {
  log('未捕获异常：', String(e))
  dialog.showErrorBox('应用异常', String(e))
})

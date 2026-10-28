// Electron 主进程：管理 Python 后端 sidecar 的生命周期，加载前端界面。
//
// 关键设计：
// 1. 后端是子进程，端口动态分配，就绪后通过 stdout 的 WRK_READY 行回报；
// 2. 渲染进程只加载本地打包的静态资源，不开放远程内容；
// 3. 数据目录放在用户 AppData，卸载重装不丢数据；
// 4. 后端异常退出时给出可操作的提示，而不是白屏。
const { app, BrowserWindow, dialog, ipcMain, shell, Menu } = require('electron')
const { spawn } = require('node:child_process')
const { existsSync, mkdirSync, appendFileSync } = require('node:fs')
const { join, dirname } = require('node:path')
const updater = require('./updater.cjs')

const HERE = __dirname
const ROOT = join(HERE, '..')
const isDev = process.env.WRK_DEV === '1'

let backend = null
let backendInfo = null
let mainWindow = null
let quitting = false
let startupError = ''

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

function resolveBackendCommand() {
  // 打包后：resources/backend/wrk-backend(.exe)
  // 开发时：直接用 venv 里的 python 跑模块
  const packaged = process.platform === 'win32'
    ? join(process.resourcesPath, 'backend', 'wrk-backend.exe')
    : join(process.resourcesPath, 'backend', 'wrk-backend')

  if (existsSync(packaged)) {
    return { cmd: packaged, args: [], cwd: dirname(packaged), packaged: true }
  }

  const venvPython = process.platform === 'win32'
    ? join(ROOT, '..', '.venv', 'Scripts', 'python.exe')
    : join(ROOT, '..', '.venv', 'bin', 'python')
  const python = process.env.WRK_PYTHON || (existsSync(venvPython) ? venvPython : 'python')
  const backendDir = join(ROOT, '..', 'backend')
  return {
    cmd: python,
    args: ['-m', 'app.desktop_entry'],
    cwd: backendDir,
    packaged: false,
  }
}

function startBackend() {
  return new Promise((resolve, reject) => {
    const paths = userDataPaths()
    for (const p of [paths.data, paths.files, paths.db, paths.secrets]) {
      mkdirSync(p, { recursive: true })
    }

    const { cmd, args, cwd, packaged } = resolveBackendCommand()
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
    }
    if (packaged) {
      // 打包后是单文件，模块已在可执行文件内
      env.WRK_PACKAGED = '1'
    } else {
      env.PYTHONPATH = cwd
    }

    backend = spawn(cmd, args, { cwd, env, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true })

    let settled = false
    let buffer = ''

    const onData = (chunk) => {
      buffer += chunk.toString('utf8')
      const lines = buffer.split(/\r?\n/)
      buffer = lines.pop() ?? ''
      for (const line of lines) {
        log('backend:', line)
        const m = line.match(/^WRK_READY (\{.*\})$/)
        if (m && !settled) {
          settled = true
          try {
            backendInfo = JSON.parse(m[1])
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
      if (!settled) {
        settled = true
        reject(failBackend(
          `后端服务启动失败（退出码 ${code}）。\n\n` +
          `命令：${cmd} ${args.join(' ')}\n工作目录：${cwd}\n\n` +
          `最近日志：\n${logLines.slice(-15).join('\n')}`))
        return
      }
      if (!quitting) {
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

function stopBackend() {
  if (!backend) return
  log('停止后端')
  try {
    backend.stdin?.end()
    backend.kill('SIGTERM')
    setTimeout(() => {
      if (backend && !backend.killed) {
        try { backend.kill('SIGKILL') } catch { /* 已退出 */ }
      }
    }, 4000)
  } catch (e) {
    log('停止后端出错：', String(e))
  }
  backend = null
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
  mainWindow.webContents.on('will-navigate', (event, url) => {
    const ok = isDev
      ? url.startsWith(process.env.WRK_DEV_URL || 'http://localhost:5173')
      : url.startsWith('file://')
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
  stopBackend()
  await new Promise((r) => setTimeout(r, 800))
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

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0 && backendInfo) createWindow()
})

app.on('before-quit', () => {
  updater.dispose()
})

app.on('window-all-closed', () => {
  quitting = true
  stopBackend()
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', () => { quitting = true; stopBackend() })

process.on('exit', () => stopBackend())
process.on('uncaughtException', (e) => {
  log('未捕获异常：', String(e))
  dialog.showErrorBox('应用异常', String(e))
})

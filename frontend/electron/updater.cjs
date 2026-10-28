// 自动更新：electron-updater + NSIS 安装包。
//
// 设计要点（踩过的坑写在前面）：
// 1. 只在打包版启用。开发模式默认跳过，避免 `electron .` 每次启动都去请求更新源；
//    需要本地联调时设 WRK_UPDATE_URL=http://127.0.0.1:8899 强制走通用源。
// 2. 更新源两种：
//    - github：electron-updater 读 app-update.yml（electron-builder 生成），
//      **仓库必须公开**，私有仓库的 Release 资产需要鉴权，终端用户拿不到；
//    - generic：任意静态 HTTPS 目录，放 latest.yml + 安装包 + .blockmap。
// 3. 检查失败不弹窗、不阻塞启动，只在「关于 → 软件更新」里显示原因；
//    只有「下载完成」才弹一次重启提示。
// 4. 自动检查默认启动 8 秒后一次 + 每 4 小时一次，可在 update-config.json 调整。
const { app, dialog, ipcMain, BrowserWindow } = require('electron')
const { existsSync, readFileSync } = require('node:fs')
const { join } = require('node:path')

let autoUpdater = null
let log = console.log
let timer = null
let manualCheck = false
let ipcReady = false

/** 当前更新状态，渲染进程通过 wrk:update-state 读取，事件通过 wrk:update 推送。 */
let state = {
  status: 'idle',              // idle | disabled | checking | available | none | downloading | ready | error
  currentVersion: app.getVersion(),
  latestVersion: '',
  percent: 0,
  message: '',
  feed: '',
  checkedAt: 0,
}

function configPath() {
  return app.isPackaged
    ? join(process.resourcesPath, 'update-config.json')
    : join(__dirname, 'update-config.json')
}

function readConfig() {
  const p = configPath()
  try {
    if (!existsSync(p)) return {}
    return JSON.parse(readFileSync(p, 'utf8'))
  } catch (e) {
    log('更新配置读取失败：', e.message)
    return {}
  }
}

function describeFeed(feed) {
  if (!feed) return ''
  if (feed.provider === 'github') return `GitHub ${feed.owner}/${feed.repo}`
  return String(feed.url || '')
}

function broadcast() {
  for (const win of BrowserWindow.getAllWindows()) {
    if (!win.isDestroyed()) win.webContents.send('wrk:update', state)
  }
}

function setState(patch) {
  state = { ...state, ...patch, currentVersion: app.getVersion(), checkedAt: Date.now() }
  broadcast()
}

/** 把 electron-updater 的英文错误翻成能指导操作的中文。 */
function explainError(err) {
  const raw = String(err?.message || err || '')
  if (/404|not found|cannot find/i.test(raw)) {
    return '更新源不可访问：GitHub 仓库需要公开，或改用 generic 静态源'
  }
  if (/ENOTFOUND|EAI_AGAIN|ECONNREFUSED|ETIMEDOUT|network|getaddrinfo/i.test(raw)) {
    return '连不上更新服务器，请检查网络或代理设置'
  }
  if (/sha512|checksum|integrity/i.test(raw)) {
    return '更新包校验失败（可能下载不完整），请稍后重试'
  }
  if (/dev|development/i.test(raw)) {
    return '开发模式不检查更新（可设 WRK_UPDATE_URL 联调）'
  }
  return raw.slice(0, 200) || '更新检查失败'
}

function registerIpc() {
  if (ipcReady) return
  ipcReady = true
  ipcMain.handle('wrk:update-state', () => state)
  ipcMain.handle('wrk:update-check', () => check(true))
  ipcMain.handle('wrk:update-install', () => {
    if (state.status !== 'ready') return { ok: false, message: '还没有下载完成的更新' }
    installNow()
    return { ok: true }
  })
}

function installNow() {
  if (!autoUpdater) return
  log('更新：退出并安装新版本')
  // isSilent=false, isForceRunAfter=true：装完自动拉起新版本
  setImmediate(() => autoUpdater.quitAndInstall(false, true))
}

async function promptInstall(info) {
  if (!mainWindowAlive()) return
  const { response } = await dialog.showMessageBox({
    type: 'info',
    title: '发现新版本',
    message: `新版本 ${info?.version || state.latestVersion} 已下载完成`,
    detail: '重启应用后完成安装。安装过程不会影响已有知识库数据。',
    buttons: ['立即重启安装', '稍后'],
    defaultId: 0,
    cancelId: 1,
  })
  if (response === 0) installNow()
}

function mainWindowAlive() {
  return BrowserWindow.getAllWindows().some((w) => !w.isDestroyed())
}

function check(manual = false) {
  if (!autoUpdater) {
    return Promise.resolve({ ok: false, message: state.message || '更新未启用' })
  }
  manualCheck = manual
  setState({ status: 'checking', message: '正在检查更新…', percent: 0 })
  return autoUpdater
    .checkForUpdates()
    .then(() => ({ ok: true }))
    .catch((err) => {
      const message = explainError(err)
      log('更新检查失败：', message)
      setState({ status: 'error', message })
      return { ok: false, message }
    })
}

function schedule(cfg) {
  if (cfg.checkOnStart !== false) {
    setTimeout(() => void check(false), 8000)
  }
  const hours = Number(cfg.checkIntervalHours) > 0 ? Number(cfg.checkIntervalHours) : 4
  timer = setInterval(() => void check(false), hours * 3600 * 1000)
  if (timer.unref) timer.unref()
}

/**
 * 初始化更新器。必须在 app.whenReady() 之后、窗口创建之后调用。
 * @param {{ log: (...args:any[]) => void }} options
 */
function init(options = {}) {
  if (options.log) log = options.log
  // 先注册 IPC：即使更新被禁用/配置错误，关于页也能读到状态而不是 invoke 报错
  registerIpc()

  if (process.env.WRK_UPDATE_DISABLE === '1') {
    setState({ status: 'disabled', message: '已通过环境变量禁用更新检查' })
    return
  }

  const cfg = readConfig()
  const devUrl = (process.env.WRK_UPDATE_URL || '').trim()

  // 开发模式：只有显式给了联调地址才继续
  if (!app.isPackaged && !devUrl && process.env.WRK_UPDATE_DEV !== '1') {
    setState({ status: 'disabled', message: '开发模式不检查更新（设 WRK_UPDATE_URL 可联调）' })
    return
  }

  try {
    // 延迟 require：electron-updater 会读 app-update.yml，开发模式下没打包也能加载
    ;({ autoUpdater } = require('electron-updater'))
  } catch (e) {
    setState({ status: 'error', message: `更新组件不可用：${e.message}` })
    return
  }

  autoUpdater.autoDownload = true
  autoUpdater.autoInstallOnAppQuit = true
  autoUpdater.allowPrerelease = Boolean(cfg.allowPrerelease)
  autoUpdater.allowDowngrade = false
  autoUpdater.logger = {
    info: (...a) => log('[update]', ...a),
    warn: (...a) => log('[update:warn]', ...a),
    error: (...a) => log('[update:error]', ...a),
    debug: () => {},
  }

  // 开发联调：electron-updater 默认拒绝在非打包环境工作，这里显式允许
  if (!app.isPackaged) autoUpdater.forceDevUpdateConfig = true

  let feed = null
  if (devUrl) {
    feed = { provider: 'generic', url: devUrl }
  } else if (cfg.provider === 'generic' && cfg.url) {
    feed = { provider: 'generic', url: String(cfg.url) }
  } else if (cfg.provider === 'github' && cfg.owner && cfg.repo) {
    feed = { provider: 'github', owner: cfg.owner, repo: cfg.repo }
  }

  if (feed) {
    try {
      autoUpdater.setFeedURL(feed)
    } catch (e) {
      setState({ status: 'error', message: `更新源配置无效：${e.message}` })
      return
    }
  } else if (app.isPackaged) {
    // 没写 update-config 时退回 electron-builder 生成的 app-update.yml
    log('更新：未提供 update-config.json，使用 app-update.yml')
  } else {
    setState({ status: 'disabled', message: '未配置更新源（update-config.json）' })
    return
  }

  setState({ status: 'idle', message: '等待检查更新', feed: describeFeed(feed) })

  autoUpdater.on('checking-for-update', () => {
    setState({ status: 'checking', message: '正在检查更新…' })
  })
  autoUpdater.on('update-available', (info) => {
    log('更新：发现新版本', info?.version)
    setState({
      status: 'available',
      latestVersion: info?.version || '',
      message: `发现新版本 ${info?.version || ''}，开始下载…`,
    })
  })
  autoUpdater.on('update-not-available', (info) => {
    log('更新：已是最新版本', info?.version)
    setState({
      status: 'none',
      latestVersion: info?.version || '',
      message: '当前已是最新版本',
      percent: 0,
    })
  })
  autoUpdater.on('download-progress', (p) => {
    const percent = Math.round(Number(p?.percent) || 0)
    setState({ status: 'downloading', percent, message: `正在下载更新 ${percent}%` })
  })
  autoUpdater.on('update-downloaded', (info) => {
    log('更新：下载完成', info?.version)
    setState({
      status: 'ready',
      latestVersion: info?.version || '',
      percent: 100,
      message: `新版本 ${info?.version || ''} 已下载，重启后安装`,
    })
    void promptInstall(info)
  })
  autoUpdater.on('error', (err) => {
    const message = explainError(err)
    log('更新出错：', message)
    setState({ status: 'error', message })
  })

  schedule(cfg)
  log('更新：已启用，源 =', describeFeed(feed) || 'app-update.yml')
}

function dispose() {
  if (timer) clearInterval(timer)
  timer = null
}

module.exports = { init, check, installNow, dispose, getState: () => state }

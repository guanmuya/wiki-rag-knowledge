#!/usr/bin/env node
// Install and launch the actual Windows release in an isolated runner profile.
import { spawn, spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const VERSION = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/

async function within(promise, timeoutMs, label) {
  let timer
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`${label} timed out`)), timeoutMs) }),
    ])
  } finally {
    clearTimeout(timer)
  }
}

function killTree(child) {
  if (!child?.pid || child.exitCode !== null) return
  if (process.platform === 'win32') {
    spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore', timeout: 10000, windowsHide: true })
  } else {
    child.kill('SIGKILL')
  }
}

function installWindows({ installer, installDir, timeoutMs, env }) {
  if (process.platform !== 'win32') throw new Error('The NSIS installer requires Windows')
  // NSIS requires /D to be the last argument and unquoted, even for spaces.
  // Keep the temporary runner path free of spaces so Node will not quote it.
  if (/\s/.test(installDir)) throw new Error('The smoke-test install directory must not contain spaces')
  return new Promise((resolve, reject) => {
    const child = spawn(installer, ['/S', `/D=${installDir}`], { env, stdio: 'ignore', windowsHide: true })
    let finished = false
    const finish = (error) => {
      if (finished) return
      finished = true
      clearTimeout(timer)
      if (error) reject(error)
      else resolve()
    }
    const timer = setTimeout(() => {
      killTree(child)
      finish(new Error('Installer timed out'))
    }, timeoutMs)
    child.once('error', (error) => finish(error))
    child.once('close', (code) => finish(code === 0 ? null : new Error(`Installer exited with code ${code}`)))
  })
}

async function launchElectron(options) {
  const { _electron } = await import('playwright')
  return _electron.launch(options)
}

async function closeApplication(app) {
  if (!app) return
  const child = app.process()
  try {
    await within(Promise.resolve().then(() => app.close()), 10000, 'Application shutdown')
  } finally {
    killTree(child)
  }
}

function isInside(parent, candidate) {
  const relative = path.relative(path.resolve(parent), path.resolve(candidate))
  return relative === '' || (relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative))
}

export async function runSmoke({
  installer, installDir, userDataDir, version, timeoutMs = 120000,
  install = installWindows, launch = launchElectron, fetch: fetchImpl = globalThis.fetch,
}) {
  if (!VERSION.test(version || '')) throw new Error('Invalid release version')
  if (![installer, installDir, userDataDir].every((value) => typeof value === 'string' && path.isAbsolute(value))) {
    throw new Error('Smoke-test paths must be absolute')
  }
  const expectedInstaller = `wiki-rag-knowledge-${version}-x64-setup.exe`
  if (path.basename(installer) !== expectedInstaller || !existsSync(installer) || !statSync(installer).isFile() || statSync(installer).size === 0) {
    throw new Error('Release installer is missing or has an unexpected name')
  }
  if (isInside(installDir, userDataDir) || isInside(userDataDir, installDir)) {
    throw new Error('Install and profile directories must be separate')
  }
  if (existsSync(installDir)) throw new Error('Smoke-test install directory already exists')
  mkdirSync(userDataDir, { recursive: true })
  const env = { ...process.env, WRK_UPDATE_DISABLE: '1' }
  delete env.ELECTRON_RUN_AS_NODE
  delete env.WRK_ADMIN_PASSWORD
  await within(Promise.resolve().then(() => install({ installer, installDir, timeoutMs, env })), timeoutMs + 1000, 'Installation')
  const executable = path.join(installDir, '企业知识库.exe')
  if (!existsSync(executable) || !statSync(executable).isFile()) throw new Error('Installed application executable is missing')
  let app
  const pendingLaunch = Promise.resolve().then(() => launch({
    executablePath: executable,
    args: [`--user-data-dir=${userDataDir}`],
    env,
    timeout: timeoutMs,
  }))
  try {
    try {
      app = await within(pendingLaunch, timeoutMs, 'Application launch')
    } catch (error) {
      pendingLaunch.then((lateApp) => closeApplication(lateApp).catch(() => {}), () => {})
      throw error
    }
    const window = await within(app.firstWindow({ timeout: timeoutMs }), timeoutMs, 'Application window')
    await within(window.waitForFunction(() => Boolean(globalThis.wrkDesktop?.getInfo), null, { timeout: timeoutMs }), timeoutMs, 'Desktop bridge')
    const info = await within(window.evaluate(async () => globalThis.wrkDesktop.getInfo()), timeoutMs, 'Desktop information')
    if (!info?.isPackaged || info.version !== version || info.backendVersion !== version) throw new Error('Installed application version does not match')
    if (typeof info.dataDir !== 'string' || !isInside(userDataDir, info.dataDir)) throw new Error('Application data is outside the isolated profile')
    if (typeof info.installDirExe !== 'string' || path.resolve(info.installDirExe).toLowerCase() !== path.resolve(executable).toLowerCase()) {
      throw new Error('Application was not launched from the test installation')
    }
    let api
    try { api = new URL(info.apiBase) } catch { throw new Error('Invalid backend URL') }
    if (api.protocol !== 'http:' || api.hostname !== '127.0.0.1' || !api.port || api.username || api.password || api.pathname !== '/' || api.search || api.hash) {
      throw new Error('Backend URL is not an isolated loopback address')
    }
    const response = await within(fetchImpl(`${api.origin}/api/health`, { signal: AbortSignal.timeout(timeoutMs) }), timeoutMs, 'Backend health check')
    if (!response.ok || (await within(response.json(), timeoutMs, 'Backend health response'))?.ok !== true) {
      throw new Error('Installed backend did not pass its health check')
    }
  } finally {
    await closeApplication(app)
  }
}

async function main() {
  const tag = process.env.GITHUB_REF_NAME || ''
  const version = process.env.RELEASE_VERSION || (tag.startsWith('v') ? tag.slice(1) : '')
  const runnerTemp = process.env.RUNNER_TEMP || ''
  const runId = process.env.GITHUB_RUN_ID || ''
  if (!VERSION.test(version) || !/^\d+$/.test(runId) || !path.isAbsolute(runnerTemp)) throw new Error('Invalid release smoke-test environment')
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
  await runSmoke({
    installer: path.join(root, 'frontend', 'release', 'updates', `wiki-rag-knowledge-${version}-x64-setup.exe`),
    installDir: path.join(runnerTemp, `wrk-release-install-${runId}`),
    userDataDir: path.join(runnerTemp, `wrk-release-profile-${runId}`),
    version,
  })
  console.log('Installed application and backend health check passed')
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => { console.error(`Release smoke test failed: ${error.message}`); process.exitCode = 1 })
}

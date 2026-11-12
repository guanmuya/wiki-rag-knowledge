// 外观档位（跟随系统 / 浅色 / 深色）在主进程这一侧的唯一职责：
// 在**创建窗口之前**就知道该用浅色底还是深色底。
//
// 为什么要单独落一个文件：窗口创建时 BrowserWindow 的 `backgroundColor` 只认
// 主进程手里的值，而用户的选择存在渲染进程的 localStorage 里（Chromium 的 LevelDB，
// 主进程没有可靠的读法）。外观是纯本地偏好，写在 userData 下的
// `appearance.json` 里最省事：几十个字节，不进数据库、不走接口、不进日志。
//
// 这个模块刻意是纯 Node（不 require('electron')），因此可以用 node 直接测；
// `frontend/tests/theme.test.mjs` 就是这么验的（读坏文件、未知取值、缺文件）。
const { mkdirSync, readFileSync, writeFileSync } = require('node:fs')
const { join } = require('node:path')

/** 与渲染进程 `src/theme.tsx` 的 `ThemeMode` 一致。 */
const MODES = ['system', 'light', 'dark']

/**
 * 窗口底色。浅色沿用改动前的 `#f5f7fa`（用户截图即基准，不顺手改）；
 * 深色与 `12-theme.css` 里深色 `--c-bg: #0f1116` 相同，启动第一帧不会跳色。
 */
const BACKGROUND = { light: '#f5f7fa', dark: '#0f1116' }

const FILE_NAME = 'appearance.json'

/** 任何不认识的值（损坏的 JSON、旧版本、手改）一律按「跟随系统」。 */
function normalizeMode(value) {
  return MODES.includes(value) ? value : 'system'
}

/** 档位 + 系统当前是否深色 → 实际窗口底色。 */
function resolveBackground(mode, systemDark) {
  const dark = mode === 'dark' || (mode === 'system' && Boolean(systemDark))
  return dark ? BACKGROUND.dark : BACKGROUND.light
}

function filePath(dataDir) {
  return join(dataDir, FILE_NAME)
}

/** 读档位。文件不存在 / 不是合法 JSON / 取值不认识，都退回「跟随系统」。 */
function readMode(dataDir) {
  try {
    const parsed = JSON.parse(readFileSync(filePath(dataDir), 'utf8'))
    return normalizeMode(parsed && parsed.mode)
  } catch {
    return 'system'
  }
}

/** 写档位。返回规范化之后的档位，调用方不必再判一次。 */
function writeMode(dataDir, mode) {
  const next = normalizeMode(mode)
  try {
    mkdirSync(dataDir, { recursive: true })
    writeFileSync(filePath(dataDir), JSON.stringify({ mode: next }), 'utf8')
  } catch {
    /* 写不进去（只读目录、磁盘满）只影响下次启动的底色，不影响本次使用 */
  }
  return next
}

module.exports = {
  MODES,
  BACKGROUND,
  FILE_NAME,
  normalizeMode,
  resolveBackground,
  filePath,
  readMode,
  writeMode,
}

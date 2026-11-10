'use strict'

/**
 * 更新源 URL 安全校验（P1-9）。
 *
 * 背景：WRK_UPDATE_URL / update-config.json 的 generic 源此前可直接指向明文
 * HTTP 地址，而 autoDownload 与 autoInstallOnAppQuit 又是全开的——中间人可以
 * 同时替换 latest.yml 与安装包并触发自动安装。 electron-updater 只校验
 * latest.yml 里的 sha512，不校验发布者签名，因此「源本身必须是可信 HTTPS」
 * 是这里唯一能落地的防线。
 *
 * 规则：
 *   - 打包版：generic 源一律 HTTPS，回环地址也不例外（真实用户环境）。
 *   - 开发版：允许 http://localhost / 127.0.0.1 / ::1 用于本地联调；
 *     其余明文源一律拒绝，并在 docs/UPDATE.md §6 记录该豁免的风险。
 *   - github 源不需要 URL 校验（electron-updater 自行走 HTTPS）。
 *
 * 纯函数，不 require electron，便于 node:test 直接覆盖。
 */

/** 是否为回环主机名（含 IPv6 方括号写法）。 */
function isLoopbackHost(hostname) {
  const h = String(hostname || '').toLowerCase().replace(/^\[|\]$/g, '')
  return h === 'localhost' || h === '127.0.0.1' || h === '::1' || h === '0:0:0:0:0:0:0:1'
}

/**
 * 判断一个 URL 能否作为 generic 更新源。
 * @param {unknown} rawUrl
 * @param {{ isPackaged?: boolean }} [options]
 * @returns {boolean}
 */
function isAllowedUpdateUrl(rawUrl, options = {}) {
  const isPackaged = Boolean(options.isPackaged)
  if (typeof rawUrl !== 'string' || !rawUrl.trim()) return false

  let parsed
  try {
    parsed = new URL(rawUrl.trim())
  } catch {
    return false
  }

  if (parsed.protocol === 'https:') {
    // 不接受 https://user:pass@host 形式，避免凭据混进 URL 后被日志/状态栏带出
    return !parsed.username && !parsed.password
  }
  if (parsed.protocol !== 'http:') return false
  // 明文 HTTP 只允许「未打包 + 回环」，用于开发者本机联调
  if (isPackaged) return false
  return isLoopbackHost(parsed.hostname)
}

/**
 * 校验一条 feed 配置。
 * @param {unknown} feed
 * @param {{ isPackaged?: boolean }} [options]
 * @returns {{ ok: boolean, reason?: string }}
 */
function validateFeed(feed, options = {}) {
  const isPackaged = Boolean(options.isPackaged)
  if (!feed || typeof feed !== 'object') return { ok: false, reason: '更新源配置为空' }

  if (feed.provider === 'github') {
    return feed.owner && feed.repo
      ? { ok: true }
      : { ok: false, reason: 'GitHub 源缺少 owner / repo' }
  }
  if (feed.provider !== 'generic') {
    return { ok: false, reason: `不支持的更新源类型：${String(feed.provider)}` }
  }
  if (!isAllowedUpdateUrl(feed.url, { isPackaged })) {
    return {
      ok: false,
      reason: isPackaged
        ? '打包版更新源必须使用 HTTPS（不允许明文 HTTP）'
        : '更新源必须使用 HTTPS；仅本机回环地址（localhost / 127.0.0.1 / ::1）允许明文 HTTP 联调',
    }
  }
  return { ok: true }
}

module.exports = { isAllowedUpdateUrl, validateFeed, isLoopbackHost }

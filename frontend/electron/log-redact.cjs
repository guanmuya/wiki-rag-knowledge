'use strict'

/**
 * 后端 stdout 转写 main.log 之前的脱敏（P1-9）。
 *
 * 主进程此前用 log('backend:', line) 把后端每一行 stdout 原样落盘；一旦后端
 * 打印「密码：xxx」这类行（含旧版 bootstrap_admin 的随机初始口令、或运维在
 * 日志里塞进凭据），明文就会进 main.log，而 preload 的 getLogs 又把最近 200 行
 * 交给兜底页展示/复制。这里在落盘前把口令值替换成占位符。
 *
 * 只脱敏「标签 + 分隔符 + 值」这种确定结构，保留行内其余排障信息；
 * 说明性文案（如「密码来自 WRK_ADMIN_PASSWORD 环境变量」）不含分隔符，原样保留。
 *
 * 纯函数，不 require electron，便于 node:test 直接覆盖。
 */

const SECRET_LABEL = /(?:初始)?(?:密码|口令|password|passwd|secret|api[_-]?key|token)/i
const SECRET_VALUE = /((?:初始)?(?:密码|口令|password|passwd|secret|api[_-]?key|token)\s*[:：=]\s*)([^\s,;，；]+)/gi

/**
 * @param {unknown} line 后端输出的一行
 * @returns {string} 可安全写入日志的一行
 */
function redactBackendLine(line) {
  const text = String(line ?? '')
  if (!SECRET_LABEL.test(text)) return text
  return text.replace(SECRET_VALUE, (_match, prefix) => `${prefix}***`)
}

module.exports = { redactBackendLine }

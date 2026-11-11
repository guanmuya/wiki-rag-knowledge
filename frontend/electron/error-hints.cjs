// 失败文案匹配器（体检清单 P2-6）。
//
// 文案本体只有一份：electron/error-hints.json。这里只负责把表里的 pattern
// 编译成正则、按顺序做「先命中先返回」。主进程 main.cjs（startup）与更新器
// updater.cjs（update）共用本文件；渲染进程在浏览器侧，用
// src/shared/userFacingErrors.ts 里同一张表的等价实现（不能 require CJS）。
const table = require('./error-hints.json')

/** 把 { pattern, flags } 编译成 RegExp；表写错时在启动阶段就暴露，而不是等用户遇到。 */
function compileRules(rules) {
  return rules.map((rule) => ({ rule, regex: new RegExp(rule.pattern, rule.flags || '') }))
}

function firstMatch(compiled, raw) {
  const text = String(raw || '')
  for (const { rule, regex } of compiled) {
    if (regex.test(text)) return rule
  }
  return null
}

const STARTUP_RULES = compileRules(table.startup.hints)
const UPDATE_RULES = compileRules(table.update.hints)

/** 后端启动失败：命中的规则；调用方自己拼 detail 与兜底。 */
function matchStartup(raw) {
  return firstMatch(STARTUP_RULES, raw)
}

/** 更新失败：命中的规则；调用方自己拼兜底。 */
function matchUpdate(raw) {
  return firstMatch(UPDATE_RULES, raw)
}

/** 启动失败兜底（也用于「后端没能启动」这类没有具体原因的标题）。 */
const STARTUP_FALLBACK = table.startup.fallback

/** 更新失败兜底（raw 也为空时才用）。 */
const UPDATE_FALLBACK = table.update.fallback

/**
 * 开发模式禁用更新的说明。更新器里「状态显示」与「错误翻译」两条路径共用这一句，
 * 免得同一句话出现两种语序。
 */
const UPDATE_DEV_DISABLED = table.update.hints.find((hint) => hint.id === 'dev-mode').message

module.exports = {
  matchStartup,
  matchUpdate,
  STARTUP_FALLBACK,
  UPDATE_FALLBACK,
  UPDATE_DEV_DISABLED,
}

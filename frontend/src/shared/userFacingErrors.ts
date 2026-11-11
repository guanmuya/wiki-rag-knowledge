/**
 * 用户可见失败文案的渲染进程入口（体检清单 P2-6）。
 *
 * 文案本体只有一份：`frontend/electron/error-hints.json` 的 `probe` 段。
 * 主进程 `electron/main.cjs`（startup 段）、更新器 `electron/updater.cjs`
 * （update 段）与这里读的是同一个文件——改一处，三个进程同时生效；
 * 任何页面或组件都不要自己再抄一份错误文案。
 *
 * 为什么文案表放在 `electron/` 下：那是主进程 require 的根目录，
 * 打包配置（`electron-builder.yml` 的 `files: electron/**`）会把它带进安装包，
 * 主进程运行时直接 require 这份 JSON；渲染进程只在构建期把它内联进 bundle，
 * 运行时不访问该目录。浏览器侧不能 require CJS，所以匹配循环在这里是第二份实现
 * （十行），文案本身仍然只有一份。
 */
import table from '../../electron/error-hints.json'

/** 兜底提示：给启动门（BackendGate）直接渲染的标题 + 下一步动作。 */
export interface FailureHint {
  id: string
  title: string
  action: string
}

interface HintRule extends FailureHint {
  pattern: string
  flags: string
}

/** 数组顺序即优先级：具体原因在前，通用兜底在后（与 JSON 里的顺序一致）。 */
const RULES: Array<{ hint: HintRule; regex: RegExp }> = table.probe.hints.map((hint) => ({
  hint,
  regex: new RegExp(hint.pattern, hint.flags),
}))

/** 主进程没给失败信息时（浏览器模式、探活超时等）按错误串挑一条人话。 */
export function probeFailureHint(raw: string): FailureHint {
  const text = raw || ''
  for (const { hint, regex } of RULES) {
    if (regex.test(text)) {
      return { id: hint.id, title: hint.title, action: hint.action }
    }
  }
  return table.probe.fallback
}

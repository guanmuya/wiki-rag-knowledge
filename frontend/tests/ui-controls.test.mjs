/**
 * 表单控件口径的回归测试（纯 Node 静态扫描，不启动浏览器）。
 *
 * 为什么值得单独测：这一批问题都是「UI 统一」自己引进来的回归，靠肉眼 review 挡不住。
 *  - `.checkbox` 的本意是 15px 的复选框本体，却被打在包住文字的 <label> 上，
 *    15px 的宽高把整行文字压成一列竖排（模型 / 供应商弹窗三处，用户截图指的就是这个）。
 *  - 全站只剩 UsersPage 两处原生 <select> 没走共享组件 Select：浏览器默认外观与
 *    其它输入框完全不是一套（用户原话「跟原始人一样」）。
 *  - 共享下拉框的箭头曾经是写死灰色的数据 URI（stroke='#878da0'），深色主题下不会跟随。
 *  - 用量页「个人 Agent 按核心」表格上方的灰字说明被用户点名删掉，但标题必须留着：
 *    它是下方表格 aria-labelledby 的目标，tests/usage-page.test.mjs 也在断言它。
 *
 * 与 tests/error-hints.test.mjs、tests/agent-no-kb.test.mjs 一样是静态扫描，不依赖浏览器。
 */
import { describe, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..')
const SRC = join(ROOT, 'src')
const UI = join(SRC, 'components', 'ui.tsx')
const CONTROLS_CSS = join(SRC, 'styles', '03-controls.css')
const BREAKDOWN = join(SRC, 'pages', 'usage', 'UsageBreakdown.tsx')

const source = (file) => readFileSync(file, 'utf8')
const label = (file) => relative(ROOT, file).replace(/\\/g, '/')

/** src 下全部前端源码（.ts / .tsx），递归。 */
function srcFiles(dir = SRC) {
  const files = []
  for (const name of readdirSync(dir)) {
    const full = join(dir, name)
    if (statSync(full).isDirectory()) files.push(...srcFiles(full))
    else if (name.endsWith('.ts') || name.endsWith('.tsx')) files.push(full)
  }
  return files
}

/** 一行里 `className="..."` 是否带某个类名（按类名词边界，不会把 check-row 当成 checkbox）。 */
function hasClass(classNameValue, name) {
  return classNameValue.split(/\s+/).filter(Boolean).includes(name)
}

describe('表单控件口径', () => {
  test('原生 <select> 只允许出现在共享组件 ui.tsx 里', () => {
    const offenders = []
    for (const file of srcFiles()) {
      if (/<select\b/.test(source(file))) offenders.push(label(file))
    }
    assert.deepEqual(
      offenders,
      ['src/components/ui.tsx'],
      '全站下拉框必须走共享组件 Select（src/components/ui.tsx），否则又会出现浏览器默认外观',
    )
  })

  test('.checkbox 只打在 <input> 上，样式规则也带元素名兜底', () => {
    const offenders = []
    for (const file of srcFiles()) {
      // 逐个标签找：标签名 + 属性串，只有属性里出现 className 才检查类名。
      for (const tag of source(file).matchAll(/<([a-zA-Z][\w.]*)((?:"[^"]*"|'[^']*'|[^>"'])*)>/g)) {
        const [, name, attrs] = tag
        const className = attrs.match(/className=(?:"([^"]*)"|\{`([^`]*)`\})/)
        const value = className ? (className[1] ?? className[2] ?? '') : ''
        if (hasClass(value, 'checkbox') && name !== 'input') {
          offenders.push(`${label(file)} 把 .checkbox 打在 <${name}> 上`)
        }
      }
    }
    assert.deepEqual(offenders, [], `${offenders.join('\n')}\n.checkbox 只给 <input> 用；包住文字的一行用 .check-row`)

    // 规则本身也要带元素名：否则哪天又有人把 .checkbox 打在 label 上，15px 宽高会再次把文字压成一列。
    const css = source(CONTROLS_CSS)
    assert.match(css, /input\.checkbox\s*\{/, '.checkbox 的样式规则必须写成 input.checkbox')
    assert.match(css, /\.check-row\s*\{/, '复选框 + 文字的一行要有共享的 .check-row')
  })

  test('下拉框箭头走已有颜色令牌，不再写死灰色', () => {
    const css = source(CONTROLS_CSS)
    assert.ok(!css.includes('%23878da0'), '下拉框箭头不能再把 #878da0 写进数据 URI')
    assert.match(
      css,
      /\.select-wrap::after\s*\{[^}]*background-color:\s*var\(--c-text-muted\)/s,
      '箭头颜色要取 --c-text-muted，深色主题换令牌才会自动跟随',
    )
  })

  test('用量页核心表：标题还在，那句灰字说明已删掉', () => {
    const text = source(BREAKDOWN)
    assert.match(text, /id="usage-cores-title"/, '标题是下方表格的 aria-labelledby 目标，不能删')
    assert.ok(text.includes('个人 Agent 按核心'), '核心表标题要保留')
    assert.ok(
      !text.includes('同一个核心的多次执行合成一行'),
      '用户点名删掉的那句灰字说明不该再出现',
    )
  })
})

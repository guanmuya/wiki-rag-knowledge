/**
 * 失败文案单一来源的回归测试（体检清单 P2-6）。
 *
 * 为什么值得单独测：
 *  - 体检清单记录的原始问题是「错误文案映射表重复三套，改一处要四处同步」。
 *    收敛后文案只剩 `electron/error-hints.json` 一份，主进程（main.cjs）、
 *    更新器（updater.cjs）、渲染进程（src/shared/userFacingErrors.ts）都读它；
 *  - 只靠人工 review 挡不住「顺手再抄一份文案」的回归，所以这里同时断言
 *    匹配结果与「源码里不再出现文案副本」；
 *  - 纯 Node：不启动浏览器、不连后端，与 E2E（tests/e2e.test.mjs）分开跑。
 */
import { describe, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..')
const require = createRequire(import.meta.url)

const hints = require('../electron/error-hints.cjs')
const table = require('../electron/error-hints.json')

/** 递归列出目录下的指定后缀文件（跳过 node_modules / dist）。 */
function collectFiles(dir, suffixes) {
  const out = []
  for (const name of readdirSync(dir)) {
    if (name === 'node_modules' || name === 'dist') continue
    const full = join(dir, name)
    if (statSync(full).isDirectory()) {
      out.push(...collectFiles(full, suffixes))
      continue
    }
    if (suffixes.some((suffix) => name.endsWith(suffix))) out.push(full)
  }
  return out
}

/** 所有用户可见文案原文（标题 / 动作 / 更新提示），用于查「有没有第二份」。 */
function allMessageTexts() {
  const texts = []
  for (const section of ['startup', 'probe']) {
    const group = table[section]
    for (const hint of group.hints) texts.push(hint.title, hint.action)
    texts.push(group.fallback.title, group.fallback.action)
  }
  for (const hint of table.update.hints) texts.push(hint.message)
  texts.push(table.update.fallback)
  return [...new Set(texts)].filter((text) => text.length >= 6)
}

describe('失败文案单一来源（P2-6）', () => {
  test('文案表结构完整：三段都有规则与兜底，id 唯一，正则都能编译', () => {
    for (const section of ['startup', 'probe']) {
      const group = table[section]
      assert.ok(Array.isArray(group.hints) && group.hints.length > 0, `${section}.hints 不能为空`)
      assert.equal(typeof group.fallback.title, 'string')
      assert.equal(typeof group.fallback.action, 'string')
      const ids = group.hints.map((hint) => hint.id)
      assert.equal(new Set(ids).size, ids.length, `${section}.hints 的 id 必须唯一`)
      for (const hint of group.hints) {
        assert.ok(hint.title.length > 0 && hint.action.length > 0, `${hint.id} 缺标题或动作`)
        assert.doesNotThrow(() => new RegExp(hint.pattern, hint.flags), `${hint.id} 的正则写错了`)
      }
    }
    assert.ok(table.update.hints.length > 0)
    assert.equal(typeof table.update.fallback, 'string')
  })

  test('后端启动失败：六类原始串各命中对应规则，顺序为「先具体后兜底」', () => {
    const cases = [
      ['EACCES: permission denied, mkdir logs', 'no-permission'],
      ['listen EADDRINUSE: address already in use 127.0.0.1:8000', 'port-in-use'],
      ['ImportError: DLL load failed: 找不到 vcruntime140.dll', 'missing-runtime'],
      ['spawn ENOENT wrk-backend.exe', 'missing-file'],
      ['后端服务在 60 秒内未就绪。', 'timeout'],
      ['后端服务启动失败（退出码 1）。', 'exited'],
    ]
    for (const [raw, expected] of cases) {
      assert.equal(hints.matchStartup(raw)?.id, expected, `原始串应命中 ${expected}`)
    }
    // 兜底文案由调用方拼装：matchStartup 返回 null，main.cjs 用 STARTUP_FALLBACK
    assert.equal(hints.matchStartup('完全认不出来的一串'), null)
    assert.equal(hints.STARTUP_FALLBACK.id, 'unknown')
    assert.equal(hints.STARTUP_FALLBACK.title, table.startup.fallback.title)
  })

  test('更新失败：四类原始串各命中对应提示，原始串也没有时用兜底', () => {
    assert.equal(hints.matchUpdate('HttpError: 404 Not Found')?.id, 'not-found')
    assert.equal(hints.matchUpdate('getaddrinfo ENOTFOUND github.com')?.id, 'network')
    assert.equal(hints.matchUpdate('sha512 checksum mismatch')?.id, 'checksum')
    assert.equal(hints.matchUpdate('dev mode: skip update check')?.id, 'dev-mode')
    assert.equal(hints.matchUpdate('莫名其妙的错误'), null)
    assert.equal(typeof hints.UPDATE_FALLBACK, 'string')
    // 开发模式说明由「状态显示」与「错误翻译」两条路径共用，必须是同一句
    assert.equal(
      hints.UPDATE_DEV_DISABLED,
      table.update.hints.find((hint) => hint.id === 'dev-mode').message,
    )
  })

  test('渲染进程读同一份表：src/shared/userFacingErrors.ts 直接 import 该 JSON', () => {
    const source = readFileSync(join(ROOT, 'src', 'shared', 'userFacingErrors.ts'), 'utf8')
    assert.match(
      source,
      /from '\.\.\/\.\.\/electron\/error-hints\.json'/,
      '渲染进程必须从 electron/error-hints.json 取文案，不能自带一份',
    )
  })

  test('源码里没有第二份文案副本（src 与 electron 下只允许出现一次）', () => {
    const texts = allMessageTexts()
    const files = [
      ...collectFiles(join(ROOT, 'src'), ['.ts', '.tsx']),
      ...collectFiles(join(ROOT, 'electron'), ['.cjs', '.mjs', '.js']),
    ]
    const offenders = []
    for (const file of files) {
      const source = readFileSync(file, 'utf8')
      for (const text of texts) {
        // 只算「整句被写成字符串字面量」的情况：日志前缀之类的包含关系不算副本
        const literal = source.includes(`'${text}'`) || source.includes(`"${text}"`)
        if (literal) offenders.push(`${file.replace(ROOT, '')} 里出现了「${text.slice(0, 12)}…」`)
      }
    }
    assert.deepEqual(offenders, [], `文案只允许写在 electron/error-hints.json 里：\n${offenders.join('\n')}`)
  })
})

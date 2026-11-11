/**
 * 个人 Agent 不接知识库的回归测试。
 *
 * 为什么值得单独测：
 *  - 用户明确要求「个人 Agent 不该和知识库交互」，检索问答由「智能问答」页负责；
 *  - 这件事只靠 review 挡不住：选择器、核心卡片、`kb_ids` 载荷、检索文案分散在
 *    AgentPage 与 pages/agent/** 的五个文件里，任何一处被顺手加回来都算回退；
 *  - 纯 Node 静态扫描：不启动浏览器、不连后端，与 E2E（tests/e2e.test.mjs）分开跑。
 *
 * 判定口径是「个人 Agent 的前端源码里不出现知识库接入面」：
 * `kbApi`（拉取知识库列表）、`kb_ids`（会话载荷与展示）、`KnowledgeBase`（类型）、
 * `kind === 'kb'`（内置检索核心的渲染分支），以及用户可见文案里的「知识库」。
 * 唯一允许出现 `kb` 的地方是核心清单的过滤（`kind !== 'kb'`）。
 */
import { describe, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..')
const PAGES = join(ROOT, 'src', 'pages')
const AGENT_DIR = join(PAGES, 'agent')

/** 个人 Agent 页的全部源码：页面本体 + pages/agent/** 下的组件与 hooks。 */
function agentPageFiles() {
  const files = [join(PAGES, 'AgentPage.tsx')]
  for (const name of readdirSync(AGENT_DIR)) {
    if (name.endsWith('.ts') || name.endsWith('.tsx')) files.push(join(AGENT_DIR, name))
  }
  return files
}

const FILES = agentPageFiles()
const source = (file) => readFileSync(file, 'utf8')
const label = (file) => relative(ROOT, file).replace(/\\/g, '/')

describe('个人 Agent 不接知识库', () => {
  test('覆盖的文件都在：页面本体与 pages/agent 下的组件、hooks', () => {
    const names = FILES.map((file) => label(file))
    for (const expected of [
      'src/pages/AgentPage.tsx',
      'src/pages/agent/AgentChat.tsx',
      'src/pages/agent/PluginPanel.tsx',
      'src/pages/agent/SessionList.tsx',
      'src/pages/agent/useAgentSessions.ts',
      'src/pages/agent/useAgentPlugins.ts',
    ]) {
      assert.ok(names.includes(expected), `静态扫描清单漏了 ${expected}`)
    }
  })

  test('源码里没有知识库接入面：kbApi / kb_ids / KnowledgeBase / kb 核心渲染分支', () => {
    const forbidden = [
      [/kbApi/, 'kbApi（个人 Agent 不再拉取知识库列表）'],
      [/kb_ids/, 'kb_ids（会话载荷与消息都不再带知识库选择）'],
      [/KnowledgeBase/, 'KnowledgeBase 类型'],
      [/kind\s*===\s*'kb'/, "kind === 'kb'（内置检索核心的渲染分支）"],
      [/id\s*===\s*'kb'/, "id === 'kb'（内置检索核心的特判）"],
    ]
    const offenders = []
    for (const file of FILES) {
      const text = source(file)
      for (const [pattern, what] of forbidden) {
        if (pattern.test(text)) offenders.push(`${label(file)} 里还有 ${what}`)
      }
    }
    assert.deepEqual(offenders, [], offenders.join('\n'))
  })

  test('用户可见文案里不出现「知识库」，也没有「检索本应用资料」的旧承诺', () => {
    const offenders = []
    for (const file of FILES) {
      const text = source(file)
      if (text.includes('知识库')) offenders.push(`${label(file)} 里还写着「知识库」`)
      if (text.includes('检索本应用的资料')) offenders.push(`${label(file)} 里还写着检索本应用的资料`)
    }
    assert.deepEqual(offenders, [], offenders.join('\n'))
  })

  test('内置检索核心在核心清单里被过滤，不会变成可选项或核心卡片', () => {
    const text = source(join(AGENT_DIR, 'useAgentPlugins.ts'))
    assert.match(
      text,
      /filter\(\(core\) => core\.kind !== 'kb'\)/,
      '核心清单必须先过滤掉内置检索核心，否则它会重新出现在核心卡片与下拉里',
    )
  })

  test('页面定位还在：本地个人 Agent + 本机 CLI 核心，不是知识库入口', () => {
    const text = source(join(PAGES, 'AgentPage.tsx'))
    assert.match(text, /本地个人 Agent/, '首屏标题要保持「本地个人 Agent」的定位')
    assert.match(text, /本机 CLI 核心/, '首屏要说清这里跑的是本机 CLI 核心')
  })
})

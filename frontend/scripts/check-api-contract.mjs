#!/usr/bin/env node
/**
 * 前后端契约一致性校验 —— 体检清单 `docs/HEALTH_CHECK.md` §4.2 **P1-5「前后端契约同步机制」**
 * 与 **P1-10「前端类型契约与错误兜底」** 的产出。
 *
 * 为什么是「校验」而不是「生成」：
 * 本项目的响应体不是 Pydantic 出参模型，而是 `backend/app/serializers.py` 里拼的 dict，
 * FastAPI 生成 OpenAPI 时这些响应体是空壳 `{}`，据此 codegen 只能产出一堆空类型；
 * 请求体（真正会 422 的那一半）才是 `schemas.py` 与 `routers/setup.py` 的 Pydantic 模型。
 * 所以这里直接把**请求体模型**当成事实源来比对前端手写类型：不需要改后端，
 * 也真的能拦住「字段增删了但两边没同步」。
 *
 * 四组校验（任一不过即退出码 1，CI 里阻断合并）：
 *   1. 请求体字段：`frontend/src/types.ts` 的载荷接口 ↔ 后端 Pydantic 模型
 *      - 前端多出后端不认的字段 → 后端会静默忽略，属于死字段或拼写错误
 *      - 后端必填字段前端漏写、或写成可选 → 运行时 422
 *   2. 枚举取值：前端字面量联合类型 ↔ 后端 `Literal[...]`
 *   3. 路由覆盖：`backend/app/routers/*.py` 每个 `@router.<method>()` 都要在 `docs/API.md` 有契约行
 *   4. 桌面桥暴露面：`frontend/electron/preload.cjs` 暴露的键 ↔ `types.ts` 的 `DesktopBridge`，
 *      以及 `main.cjs` 里 `wrk:info` 返回的字段 ↔ `DesktopInfo`
 *      （P1-10 记录的三处漂移正在这一层：漏 `getFailure`、`openPath` 漏 `'install'`、
 *       `getInfo` 只声明了 4 个字段）
 *
 * 用法：`pnpm check:contract`（在 `frontend/` 下），或 `node frontend/scripts/check-api-contract.mjs`（仓库根目录）。
 * 只读：本脚本不写任何文件。
 */
import { readFileSync, readdirSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
/** 仓库根目录（本文件在 frontend/scripts/ 下） */
const ROOT = resolve(HERE, '..', '..')
const read = (rel) => readFileSync(join(ROOT, rel), 'utf8')

const problems = []
const checks = []
const fail = (group, message) => problems.push(`[${group}] ${message}`)
const pass = (group, message) => checks.push(`[${group}] ${message}`)

// ------------------------------------------------------------------ 载荷接口 ↔ Pydantic 模型
/**
 * 前端载荷接口 → 后端请求体模型。
 * 新增接口时在这里补一行，否则它不受本校验保护（漏登记的代价只是缺一层保护，不会误报）。
 */
const PAYLOAD_MODELS = [
  ['SetupStatus', 'SetupStatusOut'],
  ['SetupAdminPayload', 'SetupAdminIn'],
  ['CreateUserPayload', 'UserCreateIn'],
  ['UpdateUserPayload', 'UserUpdateIn'],
  ['ProviderPayload', 'ProviderIn'],
  ['ModelPayload', 'ModelIn'],
  ['ModelImportItem', 'ModelImportItemIn'],
  ['ModelPricePayload', 'ModelPriceIn'],
  ['AgentSessionPayload', 'AgentSessionIn'],
]

/** 前端字面量联合类型 ↔ 后端某个模型的某个 `Literal[...]` 字段。 */
const UNION_LITERALS = [
  ['Role', 'UserCreateIn', 'role'],
  ['UserStatus', 'UserUpdateIn', 'status'],
  ['KbMode', 'KbCreateIn', 'mode'],
  ['GrantLevel', 'GrantIn', 'level'],
  ['ModelPurpose', 'ModelIn', 'purpose'],
  ['ProviderProtocol', 'ProviderIn', 'protocol'],
]

/**
 * 解析 Pydantic 模型：`class X(BaseModel):` 之后、缩进 4 空格的 `字段: 注解[ = 默认值]`。
 * 只认这两种写法——本仓库的请求体模型都是这个形态，不做通用 Python 解析。
 */
function parsePydanticModels(sources) {
  const models = new Map()
  for (const source of sources) {
    let current = null
    for (const line of source.split(/\r?\n/)) {
      const classMatch = /^class\s+([A-Za-z_]\w*)\s*\(\s*BaseModel\s*\)\s*:/.exec(line)
      if (classMatch) {
        current = { name: classMatch[1], fields: new Map() }
        models.set(current.name, current)
        continue
      }
      // 顶格的非空、非注释行（新 class / 新函数 / 顶层语句）结束当前类的字段区
      if (current && /^\S/.test(line) && !/^#/.test(line)) {
        current = null
        continue
      }
      if (!current) continue
      const fieldMatch = /^\s{4}([A-Za-z_]\w*)\s*:\s*(.+)$/.exec(line)
      if (!fieldMatch) continue
      const [, name, rest] = fieldMatch
      // 默认值一律写成「空格 等号 空格」，据此把注解与默认值分开
      const splitAt = rest.indexOf(' = ')
      const annotation = splitAt === -1 ? rest : rest.slice(0, splitAt)
      current.fields.set(name, {
        annotation: annotation.trim(),
        required: splitAt === -1,
        literals: extractLiterals(annotation),
      })
    }
  }
  return models
}

/** 从 `Literal["a", "b"]` 里取出取值列表；没有 Literal 返回 null。 */
function extractLiterals(annotation) {
  const match = /Literal\[([^\]]*)\]/.exec(annotation)
  if (!match) return null
  return [...match[1].matchAll(/["']([^"']+)["']/g)].map((item) => item[1])
}

/** 解析 TS 的 `export interface X { 字段?: 类型 }`，只取顶层字段（按花括号深度过滤嵌套对象）。 */
function parseTsInterfaces(source) {
  const interfaces = new Map()
  const lines = source.split(/\r?\n/)
  for (let i = 0; i < lines.length; i += 1) {
    const openMatch = /^export\s+interface\s+([A-Za-z_]\w*)\s*\{/.exec(lines[i])
    if (!openMatch) continue
    const fields = new Map()
    let depth = 0
    for (let j = i + 1; j < lines.length; j += 1) {
      const line = lines[j]
      if (/^\}/.test(line)) {
        i = j
        break
      }
      const opens = (line.match(/[{(\[<]/g) ?? []).length
      const closes = (line.match(/[})\]>]/g) ?? []).length
      if (depth === 0) {
        const fieldMatch = /^\s{2}([A-Za-z_]\w*)(\?)?\s*:\s*(.+)$/.exec(line)
        if (fieldMatch) {
          fields.set(fieldMatch[1], {
            optional: Boolean(fieldMatch[2]),
            annotation: fieldMatch[3].trim(),
          })
        }
      }
      depth += opens - closes
      if (depth < 0) depth = 0
    }
    interfaces.set(openMatch[1], fields)
  }
  return interfaces
}

/** 解析 TS 字面量联合类型：`export type X = 'a' | 'b'`，并支持后续行以 `|` 开头的多行写法。 */
function parseTsUnions(source) {
  const unions = new Map()
  const lines = source.split(/\r?\n/)
  for (let i = 0; i < lines.length; i += 1) {
    const match = /^export\s+type\s+([A-Za-z_]\w*)\s*=\s*(.*)$/.exec(lines[i])
    if (!match) continue
    const [, name] = match
    let body = match[2]
    // 多行联合：后续行只要以 | 开头就继续并入
    for (let j = i + 1; j < lines.length; j += 1) {
      if (/^\s*\|/.test(lines[j])) {
        body += ' ' + lines[j].trim()
        i = j
      } else break
    }
    const values = [...body.matchAll(/["']([^"']+)["']/g)].map((item) => item[1])
    if (values.length) unions.set(name, values)
  }
  return unions
}

// ------------------------------------------------------------------ 路由 ↔ API.md
/** 取后端全部路由（`/api` 前缀来自 `main.py` 的 `include_router(..., prefix="/api")`）。 */
function parseBackendRoutes() {
  const dir = 'backend/app/routers'
  const routes = []
  for (const file of readdirSync(join(ROOT, dir)).filter((name) => name.endsWith('.py'))) {
    const source = read(`${dir}/${file}`)
    for (const match of source.matchAll(
      /@router\.(get|post|put|patch|delete)\(\s*["']([^"']+)["']/g,
    )) {
      routes.push({
        method: match[1].toUpperCase(),
        path: normalizePath('/api' + match[2]),
        source: `${dir}/${file}`,
      })
    }
  }
  return routes
}

/** 路径参数名后端写 `{pid}`、文档写 `{id}`，比对时统一抹平成 `{}`。 */
function normalizePath(path) {
  return path.replace(/\{[^}]*\}/g, '{}').replace(/\?.*$/, '')
}

/** 解析 `docs/API.md` 的契约表行：`| GET | `/api/xxx` | 说明 |`。 */
function parseDocumentedRoutes() {
  const documented = new Set()
  for (const line of read('docs/API.md').split(/\r?\n/)) {
    const match = /^\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|\s*`([^`]+)`/.exec(line)
    if (match) documented.add(`${match[1]} ${normalizePath(match[2])}`)
  }
  return documented
}

// ------------------------------------------------------------------ preload ↔ DesktopBridge
/** 取 `contextBridge.exposeInMainWorld('wrkDesktop', { ... })` 的顶层键。 */
function parsePreloadKeys(source) {
  const keys = new Set()
  let inside = false
  for (const line of source.split(/\r?\n/)) {
    if (!inside) {
      if (/exposeInMainWorld\(\s*['"]wrkDesktop['"]\s*,\s*\{/.test(line)) inside = true
      continue
    }
    if (/^\}/.test(line)) break
    const match = /^\s{2}([A-Za-z_]\w*)\s*:/.exec(line)
    if (match) keys.add(match[1])
  }
  return keys
}

/**
 * 取 `ipcMain.handle('<channel>', () => ({ ... }))` 里返回对象的顶层键。
 * 同时认 `key: value` 与 ES6 简写 `key,`（`wrk:info` 的 `isDev,` 就是简写）。
 */
function parseIpcResultKeys(source, channel) {
  const keys = new Set()
  const lines = source.split(/\r?\n/)
  const opener = new RegExp(`ipcMain\\.handle\\(\\s*['"]${channel}['"]\\s*,.*\\(\\s*\\{`)
  let inside = false
  for (const line of lines) {
    if (!inside) {
      if (opener.test(line)) inside = true
      continue
    }
    if (/^\}/.test(line)) break
    const match = /^\s{2}([A-Za-z_]\w*)\s*[:,]/.exec(line)
    if (match) keys.add(match[1])
  }
  return keys
}

// ------------------------------------------------------------------ 主流程
const typesSource = read('frontend/src/types.ts')
const pydanticModels = parsePydanticModels([
  read('backend/app/schemas.py'),
  ...readdirSync(join(ROOT, 'backend/app/routers'))
    .filter((name) => name.endsWith('.py'))
    .map((name) => read(`backend/app/routers/${name}`)),
])
const tsInterfaces = parseTsInterfaces(typesSource)
const tsUnions = parseTsUnions(typesSource)

// 1) 请求体字段
for (const [tsName, modelName] of PAYLOAD_MODELS) {
  const model = pydanticModels.get(modelName)
  const fields = tsInterfaces.get(tsName)
  if (!model) {
    fail('请求体', `找不到后端模型 ${modelName}（${tsName} 的对照）——后端改名了？`)
    continue
  }
  if (!fields) {
    fail('请求体', `找不到前端接口 ${tsName}（${modelName} 的对照）——前端改名了？`)
    continue
  }
  for (const field of fields.keys()) {
    if (!model.fields.has(field)) {
      fail('请求体', `${tsName}.${field} 在后端 ${modelName} 里不存在（后端会静默忽略这个字段）`)
    }
  }
  for (const [field, spec] of model.fields) {
    const ts = fields.get(field)
    if (!ts) {
      fail(
        '请求体',
        `后端 ${modelName}.${field}（${spec.required ? '必填' : '有默认值'}）在 ${tsName} 里没有声明`,
      )
      continue
    }
    if (spec.required && ts.optional) {
      fail('请求体', `${tsName}.${field} 是可选的，但后端 ${modelName}.${field} 必填（会 422）`)
    }
  }
  pass('请求体', `${tsName} ↔ ${modelName}（${model.fields.size} 字段）`)
}

// 2) 枚举取值
for (const [unionName, modelName, field] of UNION_LITERALS) {
  const model = pydanticModels.get(modelName)
  const spec = model?.fields.get(field)
  const union = tsUnions.get(unionName)
  if (!spec || !spec.literals) {
    fail('枚举', `后端 ${modelName}.${field} 不是 Literal，无法与 ${unionName} 对照`)
    continue
  }
  if (!union) {
    fail('枚举', `找不到前端联合类型 ${unionName}`)
    continue
  }
  const onlyBackend = spec.literals.filter((value) => !union.includes(value))
  const onlyFrontend = union.filter((value) => !spec.literals.includes(value))
  if (onlyBackend.length) fail('枚举', `${unionName} 少了后端取值：${onlyBackend.join(', ')}`)
  if (onlyFrontend.length) fail('枚举', `${unionName} 多出后端不认的取值：${onlyFrontend.join(', ')}`)
  pass('枚举', `${unionName} ↔ ${modelName}.${field}（${spec.literals.length} 取值）`)
}

// 3) 路由覆盖
const backendRoutes = parseBackendRoutes()
const documentedRoutes = parseDocumentedRoutes()
for (const route of backendRoutes) {
  const key = `${route.method} ${route.path}`
  if (!documentedRoutes.has(key)) {
    fail('路由', `${route.source} 的 ${key} 在 docs/API.md 里没有契约行`)
  }
}
pass('路由', `后端 ${backendRoutes.length} 条路由，docs/API.md 已覆盖`)

// 4) 桌面桥（两层：暴露面 + getInfo 的字段）
const preloadKeys = parsePreloadKeys(read('frontend/electron/preload.cjs'))
const bridge = tsInterfaces.get('DesktopBridge')
if (!bridge) {
  fail('桌面桥', 'types.ts 里找不到 DesktopBridge 接口')
} else {
  for (const key of preloadKeys) {
    if (!bridge.has(key)) {
      fail('桌面桥', `preload.cjs 暴露了 ${key}，但 DesktopBridge 没有声明（调用处只能强转）`)
    }
  }
  for (const key of bridge.keys()) {
    if (!preloadKeys.has(key)) {
      fail('桌面桥', `DesktopBridge 声明了 ${key}，但 preload.cjs 没有暴露（运行时会 undefined）`)
    }
  }
  pass('桌面桥', `preload.cjs ↔ DesktopBridge（${preloadKeys.size} 个键）`)
}

// getInfo 的字段：主进程 wrk:info 实际返回的键 ↔ DesktopInfo
const infoKeys = parseIpcResultKeys(read('frontend/electron/main.cjs'), 'wrk:info')
const desktopInfo = tsInterfaces.get('DesktopInfo')
if (!desktopInfo) {
  fail('桌面桥', 'types.ts 里找不到 DesktopInfo 接口')
} else if (!infoKeys.size) {
  fail('桌面桥', "main.cjs 里没解析到 ipcMain.handle('wrk:info') 的返回字段")
} else {
  for (const key of infoKeys) {
    if (!desktopInfo.has(key)) {
      fail('桌面桥', `main.cjs 的 wrk:info 返回了 ${key}，但 DesktopInfo 没有声明`)
    }
  }
  for (const key of desktopInfo.keys()) {
    if (!infoKeys.has(key)) {
      fail('桌面桥', `DesktopInfo 声明了 ${key}，但 main.cjs 的 wrk:info 不返回（永远是 undefined）`)
    }
  }
  pass('桌面桥', `main.cjs 的 wrk:info ↔ DesktopInfo（${infoKeys.size} 个字段）`)
}

// ------------------------------------------------------------------ 输出
if (problems.length) {
  console.error(`\n契约校验未通过，共 ${problems.length} 处不一致：\n`)
  for (const problem of problems) console.error(`  x ${problem}`)
  console.error(
    '\n改法：前端改 frontend/src/types.ts，后端改 schemas.py / routers，' +
      '文档改 docs/API.md，两边必须同时改；详见 docs/API.md §10「契约同步与校验」。\n',
  )
  process.exit(1)
}

console.log(`契约校验通过（${checks.length} 组）：`)
for (const line of checks) console.log(`  - ${line}`)

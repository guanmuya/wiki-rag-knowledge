'use strict'

// P1-9 回归测试：更新源安全校验 + 后端 stdout 落日志前的脱敏。
// 纯 CommonJS + node:test，不依赖 Electron / node_modules，可在本机直接跑：
//   node --test frontend/electron/security.test.cjs
const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')

const {
  isAllowedUpdateUrl,
  validateFeed,
  isLoopbackHost,
} = require('./update-url.cjs')
const { redactBackendLine } = require('./log-redact.cjs')

test('打包版接受 HTTPS 源', () => {
  assert.equal(isAllowedUpdateUrl('https://cdn.example.com/wrk-updates', { isPackaged: true }), true)
})

test('打包版拒绝明文 HTTP 源', () => {
  assert.equal(isAllowedUpdateUrl('http://cdn.example.com/wrk-updates', { isPackaged: true }), false)
})

test('打包版连回环明文源也拒绝（真实用户环境不允许明文更新源）', () => {
  assert.equal(isAllowedUpdateUrl('http://127.0.0.1:8899', { isPackaged: true }), false)
})

test('开发模式允许回环明文源用于本地联调', () => {
  assert.equal(isAllowedUpdateUrl('http://127.0.0.1:8899', { isPackaged: false }), true)
  assert.equal(isAllowedUpdateUrl('http://localhost:8899', { isPackaged: false }), true)
  assert.equal(isAllowedUpdateUrl('http://[::1]:8899', { isPackaged: false }), true)
})

test('开发模式拒绝非回环明文源', () => {
  assert.equal(isAllowedUpdateUrl('http://192.168.1.10:8899', { isPackaged: false }), false)
  assert.equal(isAllowedUpdateUrl('http://lan.example.com/updates', { isPackaged: false }), false)
})

test('拒绝非 HTTP(S) 协议、畸形 URL 与空值', () => {
  assert.equal(isAllowedUpdateUrl('ftp://cdn.example.com/updates'), false)
  assert.equal(isAllowedUpdateUrl('file:///C:/updates'), false)
  assert.equal(isAllowedUpdateUrl('not a url'), false)
  assert.equal(isAllowedUpdateUrl(''), false)
  assert.equal(isAllowedUpdateUrl(null), false)
})

test('回环主机名识别覆盖常见写法', () => {
  assert.equal(isLoopbackHost('127.0.0.1'), true)
  assert.equal(isLoopbackHost('LOCALHOST'), true)
  assert.equal(isLoopbackHost('[::1]'), true)
  assert.equal(isLoopbackHost('example.com'), false)
})

test('validateFeed 放行合规 generic 与 github 源', () => {
  assert.deepEqual(validateFeed({ provider: 'generic', url: 'https://cdn.example.com/u' }, { isPackaged: true }), { ok: true })
  assert.deepEqual(validateFeed({ provider: 'github', owner: 'o', repo: 'r' }, { isPackaged: true }), { ok: true })
})

test('validateFeed 拦截不安全源并给出可读原因', () => {
  const bad = validateFeed({ provider: 'generic', url: 'http://cdn.example.com/u' }, { isPackaged: true })
  assert.equal(bad.ok, false)
  assert.match(bad.reason, /HTTPS/)
  assert.equal(validateFeed({ provider: 'generic', url: '' }, { isPackaged: true }).ok, false)
  assert.equal(validateFeed({ provider: 'github', owner: '', repo: '' }, { isPackaged: true }).ok, false)
  assert.equal(validateFeed(null, { isPackaged: true }).ok, false)
})

test('redactBackendLine 脱敏「密码：」后的口令值', () => {
  assert.equal(redactBackendLine('  密码：S3cr3t!'), '  密码：***')
  assert.equal(redactBackendLine('  初始密码: abc123'), '  初始密码: ***')
  assert.equal(redactBackendLine('  password=abc123'), '  password=***')
  assert.equal(redactBackendLine('  API_KEY：sk-123456'), '  API_KEY：***')
})

test('redactBackendLine 不改动普通日志与说明性文案', () => {
  assert.equal(redactBackendLine('WRK_READY {"port":8000}'), 'WRK_READY {"port":8000}')
  assert.equal(redactBackendLine('首次部署：已创建管理员账号'), '首次部署：已创建管理员账号')
  assert.equal(redactBackendLine('密码来自 WRK_ADMIN_PASSWORD 环境变量'), '密码来自 WRK_ADMIN_PASSWORD 环境变量')
  assert.equal(redactBackendLine(''), '')
  assert.equal(redactBackendLine(null), '')
})

test('交付配置里没有明文 HTTP 更新源（P1-9 回归）', () => {
  const dev = readFileSync(join(__dirname, '..', 'dev-app-update.yml'), 'utf8')
  const m = dev.match(/^\s*url:\s*(\S+)/m)
  assert.ok(m, 'dev-app-update.yml 应有 url 字段')
  assert.equal(m[1].startsWith('http://'), false, `dev-app-update.yml 仍是明文源：${m[1]}`)

  const cfg = JSON.parse(readFileSync(join(__dirname, 'update-config.json'), 'utf8'))
  const urls = []
  if (typeof cfg.url === 'string') urls.push(cfg.url)
  for (const s of Array.isArray(cfg.sources) ? cfg.sources : []) {
    if (s && typeof s.url === 'string') urls.push(s.url)
  }
  assert.equal(urls.some((u) => u.startsWith('http://')), false, 'update-config.json 含明文 HTTP 源')
})

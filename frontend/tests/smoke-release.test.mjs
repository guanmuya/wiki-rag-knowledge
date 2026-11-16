import assert from 'node:assert/strict'
import { mkdtemp, mkdir, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'

import { runSmoke } from '../scripts/smoke-release.mjs'

async function fixture() {
  const root = await mkdtemp(path.join(os.tmpdir(), 'wrk-release-smoke-'))
  const installer = path.join(root, 'wiki-rag-knowledge-1.0.23-x64-setup.exe')
  const installDir = path.join(root, 'install')
  const userDataDir = path.join(root, 'profile')
  await writeFile(installer, 'installer')
  const state = { closed: 0, launches: 0, updaterDisabled: false }
  const info = {
    isPackaged: true,
    version: '1.0.23',
    backendVersion: '1.0.23',
    apiBase: 'http://127.0.0.1:34567',
    dataDir: userDataDir,
    installDirExe: path.join(installDir, '企业知识库.exe'),
  }
  const page = {
    waitForFunction: async () => {},
    evaluate: async () => info,
  }
  const app = {
    firstWindow: async () => page,
    close: async () => { state.closed += 1 },
    process: () => null,
  }
  const install = async () => {
    await mkdir(installDir, { recursive: true })
    await writeFile(info.installDirExe, 'installed executable')
  }
  const launch = async (options) => {
    state.launches += 1
    state.updaterDisabled = options.env.WRK_UPDATE_DISABLE === '1'
    return app
  }
  const fetch = async () => ({ ok: true, json: async () => ({ ok: true }) })
  return { root, installer, installDir, userDataDir, state, info, page, app, install, launch, fetch }
}

test('checks a packaged app and its loopback backend with updates disabled', async () => {
  const f = await fixture()
  await runSmoke({ ...f, version: '1.0.23', timeoutMs: 500 })
  assert.equal(f.state.launches, 1)
  assert.equal(f.state.closed, 1)
  assert.equal(f.state.updaterDisabled, true)
})

test('does not launch an app when the installer is missing', async () => {
  const f = await fixture()
  await assert.rejects(runSmoke({ ...f, installer: path.join(f.root, 'missing.exe'), version: '1.0.23', timeoutMs: 100 }))
  assert.equal(f.state.launches, 0)
})

test('does not launch an app when installation fails', async () => {
  const f = await fixture()
  await assert.rejects(runSmoke({ ...f, install: async () => { throw new Error('installation failed') }, version: '1.0.23', timeoutMs: 100 }))
  assert.equal(f.state.launches, 0)
})

test('closes the app and rejects a non-loopback backend or mismatched version', async () => {
  for (const mutation of [
    (info) => { info.apiBase = 'http://example.com:1234' },
    (info) => { info.backendVersion = '1.0.22' },
    (info) => { info.dataDir = path.dirname(info.dataDir) },
  ]) {
    const f = await fixture()
    mutation(f.info)
    await assert.rejects(runSmoke({ ...f, version: '1.0.23', timeoutMs: 100 }))
    assert.equal(f.state.closed, 1)
  }
})

test('closes the launched app when a window never becomes ready', async () => {
  const f = await fixture()
  f.app.firstWindow = () => new Promise(() => {})
  await assert.rejects(runSmoke({ ...f, version: '1.0.23', timeoutMs: 30 }), /timed out/i)
  assert.equal(f.state.closed, 1)
})

test('closes the app when the backend health check fails', async () => {
  const f = await fixture()
  f.fetch = async () => ({ ok: true, json: async () => ({ ok: false }) })
  await assert.rejects(runSmoke({ ...f, version: '1.0.23', timeoutMs: 100 }), /health check/i)
  assert.equal(f.state.closed, 1)
})

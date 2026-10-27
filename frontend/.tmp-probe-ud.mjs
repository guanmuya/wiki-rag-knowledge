import { _electron as electron } from 'playwright'
import { existsSync, readFileSync } from 'node:fs'
const env = { ...process.env }
delete env.ELECTRON_RUN_AS_NODE
const exe = 'F:/GItHub_Code/Wiki-Rag-Knowledge/frontend/release/win-unpacked/企业知识库.exe'
const app = await electron.launch({
  executablePath: exe,
  args: ['--user-data-dir=F:/GItHub_Code/Wiki-Rag-Knowledge/.tmp-verify/ud1'],
  env,
  timeout: 120000,
})
const win = await app.firstWindow({ timeout: 90000 })
console.log('window url:', win.url())
await win.waitForTimeout(3000)
console.log('title:', await win.title())
console.log('body head:', (await win.evaluate(() => document.body.innerText)).slice(0, 400))
const logPath = 'F:/GItHub_Code/Wiki-Rag-Knowledge/.tmp-verify/ud1/logs/main.log'
console.log('log exists:', existsSync(logPath))
if (existsSync(logPath)) console.log(readFileSync(logPath, 'utf8').split('\n').slice(0, 6).join('\n'))
await app.close()

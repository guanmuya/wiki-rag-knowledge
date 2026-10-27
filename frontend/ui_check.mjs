// 真实浏览器验收：登录 → 各页面截图 → 提问 → 检查证据与路由展示。
// 用法：node scripts/ui_check.mjs [--base http://localhost:5173]
import { chromium } from 'playwright';
import { mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = resolve(HERE, '..', 'docs', 'screenshots');
mkdirSync(OUT, { recursive: true });

const BASE = process.env.UI_BASE || 'http://localhost:5173';
const USER = process.env.UI_USER || 'admin';
const PASS = process.env.UI_PASS || '';

const problems = [];

function watch(page, tag) {
  page.on('console', (msg) => {
    if (msg.type() === 'error') {
      const t = msg.text();
      if (t.includes('Download the React DevTools')) return;
      problems.push(`[${tag}] console: ${t.slice(0, 200)}`);
    }
  });
  page.on('pageerror', (err) => problems.push(`[${tag}] pageerror: ${String(err).slice(0, 200)}`));
  page.on('response', (res) => {
    if (res.status() >= 500) problems.push(`[${tag}] ${res.status()} ${res.url()}`);
  });
}

const shot = async (page, name) => {
  await page.waitForTimeout(700);
  await page.screenshot({ path: resolve(OUT, `${name}.png`), fullPage: false });
  console.log(`  截图 docs/screenshots/${name}.png`);
};

const run = async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' });
  const page = await ctx.newPage();
  watch(page, 'main');

  console.log('1) 登录页');
  await page.goto(`${BASE}/login`, { waitUntil: 'networkidle' });
  await shot(page, '01-login');

  await page.fill('input[name="username"], input[autocomplete="username"], input[type="text"]', USER);
  await page.fill('input[type="password"]', PASS);
  await page.click('button[type="submit"]');
  await page.waitForURL((u) => !u.pathname.includes('/login'), { timeout: 15000 });
  console.log('   登录成功 →', new URL(page.url()).pathname);
  await shot(page, '02-kb-list');

  console.log('2) 知识库列表');
  const kbCards = await page.locator('a[href^="/kbs/"], [data-kb-id], tr').count();
  console.log(`   可见条目数：${kbCards}`);

  console.log('3) 知识库详情');
  const firstKb = page.locator('a[href^="/kbs/"]').first();
  if (await firstKb.count()) {
    await firstKb.click();
    await page.waitForTimeout(1500);
    await shot(page, '03-kb-detail-docs');

    const wikiTab = page.getByRole('button', { name: /Wiki/i }).first();
    if (await wikiTab.count()) {
      await wikiTab.click();
      await page.waitForTimeout(1500);
      await shot(page, '04-wiki-tab');
      const pageLink = page.locator('[class*="wiki"] a, [class*="tree"] button, [class*="tree"] a').first();
      if (await pageLink.count()) {
        await pageLink.click();
        await page.waitForTimeout(1200);
        await shot(page, '05-wiki-page');
      }
    }
    const setTab = page.getByRole('button', { name: /设置/i }).first();
    if (await setTab.count()) {
      await setTab.click();
      await page.waitForTimeout(1000);
      await shot(page, '06-kb-settings');
    }
  }

  console.log('4) 问答页');
  await page.goto(`${BASE}/chat`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1200);
  await shot(page, '07-chat-empty');

  // 选知识库
  const kbChecks = page.locator('input[type="checkbox"]');
  const n = await kbChecks.count();
  for (let i = 0; i < Math.min(n, 2); i++) {
    await kbChecks.nth(i).check({ force: true }).catch(() => {});
  }
  const box = page.locator('textarea, input[placeholder*="问"], input[placeholder*="输入"]').first();
  if (await box.count()) {
    await box.fill('一线城市出差住宿上限是多少？P0 工单多久响应？');
    const send = page.getByRole('button', { name: /发送|提问|提交/ }).first();
    if (await send.count()) {
      await send.click();
    } else {
      await box.press('Enter');
    }
    await page.waitForTimeout(6000);
    await shot(page, '08-chat-answer');
    const bodyText = await page.locator('body').innerText();
    const checks = {
      '有回答文本': /住宿|600|工单|P0/.test(bodyText),
      '显示证据/来源': /来源|证据|引用|原文/.test(bodyText),
      '显示路由信息': /Wiki|分块|检索/.test(bodyText),
    };
    for (const [k, v] of Object.entries(checks)) console.log(`   ${v ? '✓' : '✗'} ${k}`);
    if (!checks['有回答文本']) problems.push('问答页未显示预期回答内容');
  } else {
    problems.push('问答页找不到输入框');
  }

  console.log('5) 管理页面');
  for (const [path, name] of [['/admin/users', '09-users'], ['/admin/models', '10-models'], ['/tasks', '11-tasks']]) {
    await page.goto(`${BASE}${path}`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(1500);
    await shot(page, name);
    const t = await page.locator('body').innerText();
    if (t.length < 50) problems.push(`${path} 页面内容为空`);
  }

  console.log('6) 普通员工视角');
  const ctx2 = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' });
  const p2 = await ctx2.newPage();
  watch(p2, 'employee');
  await p2.goto(`${BASE}/login`, { waitUntil: 'networkidle' });
  await p2.fill('input[name="username"], input[autocomplete="username"], input[type="text"]', 'lisi');
  await p2.fill('input[type="password"]', process.env.UI_EMP_PASS || '');
  await p2.click('button[type="submit"]');
  await p2.waitForTimeout(2500);
  await shot(p2, '12-employee-view');
  const empText = await p2.locator('body').innerText();
  const adminVisible = /用户管理|模型设置/.test(empText);
  console.log(`   ${adminVisible ? '✗ 员工仍看到管理入口' : '✓ 员工看不到管理入口'}`);
  if (adminVisible) problems.push('普通员工侧边栏仍显示管理菜单');

  await p2.goto(`${BASE}/admin/users`, { waitUntil: 'networkidle' });
  await p2.waitForTimeout(1200);
  const blocked = !(await p2.locator('body').innerText()).includes('新建用户');
  console.log(`   ${blocked ? '✓ 直接访问 /admin/users 被拦截' : '✗ 员工可直达管理页'}`);
  if (!blocked) problems.push('员工可直接访问 /admin/users');

  await browser.close();

  console.log('\n=== 结果 ===');
  if (problems.length) {
    console.log(`发现 ${problems.length} 个问题：`);
    problems.forEach((p) => console.log('  -', p));
    process.exitCode = 1;
  } else {
    console.log('未发现前端运行时错误');
  }
};

run().catch((e) => {
  console.error('验收脚本异常：', e);
  process.exitCode = 2;
});

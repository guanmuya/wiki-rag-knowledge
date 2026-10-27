// 首次初始化流程的真实浏览器验收。
// 用法：node frontend/setup_check.mjs
import { chromium } from 'playwright';
import { mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = resolve(HERE, '..', 'docs', 'screenshots');
mkdirSync(OUT, { recursive: true });

const BASE = process.env.UI_BASE || 'http://localhost:5173';
const problems = [];
const shot = async (page, name) => {
  await page.waitForTimeout(600);
  await page.screenshot({ path: resolve(OUT, `${name}.png`) });
  console.log(`  截图 ${name}.png`);
};

const run = async () => {
  const browser = await chromium.launch({ channel: 'chrome', args: ['--no-sandbox'] });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' });
  const page = await ctx.newPage();
  page.on('pageerror', (e) => problems.push(`pageerror: ${String(e).slice(0, 150)}`));
  page.on('console', (m) => {
    if (m.type() === 'error' && !m.text().includes('DevTools')) {
      problems.push(`console: ${m.text().slice(0, 150)}`);
    }
  });

  console.log('1) 访问根路径，应被重定向到 /setup');
  await page.goto(`${BASE}/`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);
  const url = page.url();
  console.log('   当前 URL:', url);
  const onSetup = url.includes('#/setup') || url.includes('/setup');
  console.log(`   ${onSetup ? '✓' : '✗'} 重定向到初始化页`);
  if (!onSetup) problems.push(`未重定向到 /setup，实际 ${url}`);

  const bodyText = await page.locator('body').innerText();
  const hasTitle = /初始化系统|创建第一个管理员/.test(bodyText);
  console.log(`   ${hasTitle ? '✓' : '✗'} 页面标题正确`);
  if (!hasTitle) problems.push('初始化页标题缺失');
  await shot(page, '13-setup-page');

  console.log('2) 弱密码应被前端拦截');
  const inputs = page.locator('input');
  const n = await inputs.count();
  console.log(`   输入框数量: ${n}`);
  // 企业名称、用户名、显示名、密码、确认密码
  if (n >= 5) {
    await inputs.nth(0).fill('测试科技有限公司');
    await inputs.nth(1).fill('boss');
    await inputs.nth(2).fill('张管理');
    await inputs.nth(3).fill('weak');
    await inputs.nth(4).fill('weak');
    const submit = page.getByRole('button', { name: /创建管理员/ });
    await submit.click();
    await page.waitForTimeout(800);
    const errText = await page.locator('body').innerText();
    const blocked = /至少 10 位|三类|不一致/.test(errText);
    console.log(`   ${blocked ? '✓' : '✗'} 弱密码被拦截`);
    if (!blocked) problems.push('弱密码未被拦截');
    await shot(page, '14-setup-weak-password');
  } else {
    problems.push(`初始化页输入框数量异常：${n}`);
  }

  console.log('3) 两次密码不一致应被拦截');
  await inputs.nth(3).fill('Setup#Passw0rd9');
  await inputs.nth(4).fill('Different#123');
  await page.getByRole('button', { name: /创建管理员/ }).click();
  await page.waitForTimeout(600);
  const mismatch = await page.locator('body').innerText();
  const mismatchBlocked = /不一致/.test(mismatch);
  console.log(`   ${mismatchBlocked ? '✓' : '✗'} 密码不一致被拦截`);
  if (!mismatchBlocked) problems.push('密码不一致未被拦截');

  console.log('4) 正确填写应创建成功并跳到登录页');
  await inputs.nth(4).fill('Setup#Passw0rd9');
  await page.getByRole('button', { name: /创建管理员/ }).click();
  await page.waitForTimeout(3500);
  const afterUrl = page.url();
  console.log('   当前 URL:', afterUrl);
  const onLogin = afterUrl.includes('#/login') || afterUrl.includes('/login');
  console.log(`   ${onLogin ? '✓' : '✗'} 创建成功并跳转登录页`);
  if (!onLogin) problems.push(`创建后未跳登录页，实际 ${afterUrl}`);
  await shot(page, '15-setup-done-login');

  console.log('5) 用新建账号登录');
  await page.fill('input[autocomplete="username"], input[type="text"]', 'boss');
  await page.fill('input[type="password"]', 'Setup#Passw0rd9');
  await page.click('button[type="submit"]');
  await page.waitForTimeout(3000);
  const loggedIn = page.url().includes('/kbs');
  console.log(`   ${loggedIn ? '✓' : '✗'} 登录成功进入系统`);
  if (!loggedIn) problems.push(`新建管理员登录失败，URL=${page.url()}`);
  await shot(page, '16-setup-login-success');

  console.log('6) 再访问 /setup 应被送回登录页');
  await page.goto(`${BASE}/#/setup`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);
  const backUrl = page.url();
  const kicked = !backUrl.includes('/setup') || backUrl.includes('/login');
  console.log(`   ${kicked ? '✓' : '✗'} 已初始化后 /setup 不可再进`);
  if (!kicked) problems.push('已初始化仍可进入 /setup');

  await browser.close();

  console.log('\n=== 结果 ===');
  if (problems.length) {
    console.log(`发现 ${problems.length} 个问题：`);
    problems.forEach((p) => console.log('  -', p));
    process.exitCode = 1;
  } else {
    console.log('初始化流程全部通过');
  }
};

run().catch((e) => {
  console.error('验收脚本异常：', e);
  process.exitCode = 2;
});

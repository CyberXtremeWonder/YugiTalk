const { chromium, expect } = require('../frontend/node_modules/@playwright/test');
const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const root = path.resolve(__dirname, '..');
const origin = 'http://127.0.0.1:18010';
const output = path.join(root, 'docs/stages/artifacts');

/** 等待仅回环的隔离服务就绪，不复用真实开发身份或数据库。 */
async function waitReady(child) {
  for (let n = 0; n < 80; n++) {
    if (child.exitCode !== null) throw new Error('隔离服务提前退出');
    try { if ((await fetch(origin + '/health/live')).ok) return; } catch { /* 等待启动。 */ }
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  throw new Error('隔离服务启动超时');
}

/** 验证注册、模式权限和工具提交审核，邮件与网络均采用隔离测试服务。 */
async function main() {
  const child = spawn(path.join(root, 'backend/.venv/Scripts/python.exe'), [path.join(root, 'scripts/registration-ui-fixture.py')], { cwd: root, env: { ...process.env, PYTHONUTF8: '1' }, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
  let browser;
  try {
    await waitReady(child);
    browser = await chromium.launch({ channel: 'msedge', headless: true });
    const page = await browser.newPage({ viewport: { width: 1366, height: 950 }, timezoneId: 'Asia/Shanghai' });
    const errors = [];
    const external = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => { if (new URL(request.url()).hostname !== '127.0.0.1') external.push(request.url()); });
    await page.goto(origin);
    await expect(page.getByRole('heading', { name: '欢迎回来。' })).toBeVisible();
    await page.screenshot({ path: path.join(output, 'hermes-login-desktop.png') });
    for (const width of [320, 390]) {
      await page.setViewportSize({ width, height: 844 });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await expect(page.getByRole('button', { name: '登录', exact: true })).toBeVisible();
    }
    await page.screenshot({ path: path.join(output, 'hermes-login-mobile.png') });
    await page.setViewportSize({ width: 1366, height: 950 });
    await page.getByRole('button', { name: '切换到注册' }).click();
    await page.getByLabel('邮箱', { exact: true }).fill('new-ui@example.test');
    await page.getByLabel('显示名称', { exact: true }).fill('注册验收用户');
    await page.getByRole('button', { name: '获取验证码', exact: true }).click();
    await expect(page.getByRole('status')).toContainText('验证码将发送');
    const code = (await (await page.request.get(origin + '/test-only/code?email=new-ui@example.test')).json()).code;
    await page.getByLabel('邮箱验证码', { exact: true }).fill(code);
    await page.getByLabel('密码', { exact: true }).fill('test-register-only');
    await page.getByLabel('确认密码', { exact: true }).fill('test-register-only');
    await page.getByRole('button', { name: '验证并注册', exact: true }).click();
    await expect(page.getByRole('status')).toContainText('注册成功');
    await page.getByLabel('密码', { exact: true }).fill('test-register-only');
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await expect(page.getByRole('button', { name: '新建对话' })).toBeVisible();
    await expect(page.getByRole('button', { name: '⌘ 管理员面板' })).toHaveCount(0);
    await page.getByRole('button', { name: 'Tool', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Tool', exact: true })).toBeVisible();
    await page.getByRole('button', { name: '＋ 提交工具审核', exact: true }).click();
    await page.getByLabel('工具标识', { exact: true }).fill('review_lookup');
    await page.getByLabel('显示名称', { exact: true }).fill('审核测试查询');
    await page.getByLabel('用途说明', { exact: true }).fill('只读公开信息查询');
    await page.getByLabel('HTTPS 查询地址', { exact: true }).fill('https://api.example.com/search');
    await page.getByRole('button', { name: '提交审核', exact: true }).click();
    await expect(page.getByLabel('我的工具提交')).toContainText('待审核');
    await page.getByRole('button', { name: '退出登录', exact: true }).click();
    await page.getByLabel('邮箱', { exact: true }).fill('uiadmin');
    await page.getByLabel('密码', { exact: true }).fill('test-ui-only');
    await page.getByLabel('开发模式（仅管理员）').uncheck();
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await page.getByRole('button', { name: '⌘ 管理员面板', exact: true }).click();
    await expect(page.getByRole('heading', { name: '工具提交审核', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: '审核测试查询 · review_lookup' })).toBeVisible();
    await page.getByRole('button', { name: '批准', exact: true }).click();
    await expect(page.getByRole('status')).toContainText('已批准');
    await expect(page.getByRole('heading', { name: '账号管理', exact: true })).toBeVisible();
    await page.getByRole('heading', { name: '管理员面板', exact: true }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, 'registration-admin-desktop.png') });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: path.join(output, 'registration-admin-mobile.png') });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.setViewportSize({ width: 1366, height: 950 });
    await page.getByRole('button', { name: 'Tool', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Tool', exact: true })).toBeVisible();
    await expect(page.locator('.agent-list')).not.toContainText('审核测试查询');
    await page.getByRole('button', { name: '退出登录', exact: true }).click();
    await page.getByLabel('邮箱', { exact: true }).fill('new-ui@example.test');
    await page.getByLabel('密码', { exact: true }).fill('test-register-only');
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await page.getByRole('button', { name: 'Tool', exact: true }).click();
    await expect(page.locator('.agent-list')).toContainText('审核测试查询');
    await expect(page.locator('.agent-list')).toContainText('个人');
    // 同一隔离会话验证新聊天布局、主题及手机视口，不触碰真实模型。
    await page.getByRole('button', { name: '新建对话' }).click();
    await expect(page.locator('.composer')).toBeVisible();
    await expect(page.locator('.status-footer')).not.toContainText('阶段');
    await page.screenshot({ path: path.join(output, 'hermes-chat-desktop.png') });
    await page.getByLabel('切换到深色主题').click();
    await page.screenshot({ path: path.join(output, 'hermes-chat-dark.png') });
    await page.getByLabel('切换到浅色主题').click();
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(page.locator('.composer')).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: path.join(output, 'hermes-chat-mobile.png') });
    // 小屏幕分别检查输入区和菜单，保证窄手机也能访问完整导航。
    for (const width of [320, 360]) {
      await page.setViewportSize({ width, height: 740 });
      await expect(page.locator('.composer')).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.locator('#menu').click();
      await expect(page.locator('#sidebar')).toBeVisible();
      await page.getByRole('button', { name: '设置', exact: true }).click();
      await expect(page.getByRole('heading', { name: '个人记忆', exact: true })).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.locator('#menu').click();
      await page.getByRole('button', { name: '新建对话' }).click();
    }
    expect(errors).toEqual([]);
    expect(await page.getByLabel('当前工作空间').count()).toBe(0);
    expect(external).toEqual([]);
    fs.writeFileSync(path.join(output, 'registration-ui-validation.json'), JSON.stringify({ passed: true, database: 'isolated SQLite', smtp: 'in-memory test double', external_requests: 0, real_business_data_touched: false, checks: ['验证码注册', '普通账号不显示管理员面板', '用户提交待审工具', '管理员用户名和开发模式', '管理员批准', '账号与审计展示', '390px无横向溢出'] }, null, 2));
    console.log('注册、开发模式和工具审核浏览器验收通过，未发送真实邮件。');
  } finally { if (browser) await browser.close(); child.kill(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });

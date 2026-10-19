const { chromium, expect } = require('../frontend/node_modules/@playwright/test');
const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const root = path.resolve(__dirname, '..');
const output = path.join(root, 'docs/stages/artifacts');

/** 等待真实隔离 API 启动，不连接开发数据库或外部供应商。 */
async function ready(child) {
  for (let attempt = 0; attempt < 80; attempt++) {
    if (child.exitCode !== null) throw new Error('隔离服务提前退出');
    try { if ((await fetch('http://127.0.0.1:18009/health/live')).ok) return; } catch { /* 初始化期间继续等待。 */ }
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  throw new Error('服务启动超时');
}

/** 验证工具 CRUD、授权目录、只读内置接口和移动端布局，未发出真实网络查询。 */
async function main() {
  const child = spawn(path.join(root, 'backend/.venv/Scripts/python.exe'), [path.join(root, 'scripts/custom-skills-ui-fixture.py')],
    { cwd: root, env: { ...process.env, PYTHONUTF8: '1' }, windowsHide: true, stdio: 'pipe' });
  let browser;
  let log = '';
  child.stderr.on('data', data => { log += data.toString(); });
  try {
    await ready(child);
    browser = await chromium.launch({ channel: 'msedge', headless: true });
    const page = await browser.newPage({ viewport: { width: 1366, height: 1000 } });
    const errors = [];
    const external = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => { if (new URL(request.url()).hostname !== '127.0.0.1') external.push(request.url()); });
    await page.goto('http://127.0.0.1:18009/');
    await page.getByLabel('邮箱', { exact: true }).fill('skill-ui@example.test');
    await page.getByLabel('密码', { exact: true }).fill('skill-ui-test-only');
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await page.getByRole('button', { name: 'Tool', exact: true }).click();
    await expect(page.locator('.agent-list')).toContainText('天气查询');
    await expect(page.locator('.agent-list')).toContainText('路况查询');
    await expect(page.locator('.agent-list')).toContainText('网页搜索');
    await page.getByRole('button', { name: /天气查询.*待配置/ }).click();
    await expect(page.getByRole('heading', { name: '天气查询' })).toBeVisible();
    await expect(page.getByRole('button', { name: '删除 Tool', exact: true })).toHaveCount(0);
    await expect(page.getByLabel('HTTPS 查询地址', { exact: true })).toHaveCount(0);
    await page.screenshot({ path: path.join(output, 'tools-default-weather.png') });
    await page.getByRole('button', { name: '停用 Tool', exact: true }).click();
    await expect(page.getByRole('status')).toContainText('已停用');
    await page.getByRole('button', { name: '启用 Tool', exact: true }).click();
    await expect(page.getByRole('status')).toContainText('已启用');
    await page.getByRole('button', { name: '＋ 新工具', exact: true }).click();
    await page.getByLabel('工具标识', { exact: true }).fill('public_lookup');
    await page.getByLabel('显示名称', { exact: true }).fill('公开查询验收');
    await page.getByLabel('用途说明', { exact: true }).fill('只读公开资料查询');
    await page.getByLabel('HTTPS 查询地址', { exact: true }).fill('https://api.example.com/search');
    await page.getByLabel('参数定义（JSON 数组）', { exact: true }).fill('[{"name":"q","type":"string","description":"关键词","required":true}]');
    await page.getByRole('button', { name: '创建 Tool', exact: true }).click();
    await expect(page.getByRole('status')).toContainText('v1');
    await expect(page.locator('.tool-editor')).toContainText('尚未配置服务端域名白名单');
    await page.getByLabel('示例参数（仅校验）', { exact: true }).fill('{"q":"验收"}');
    await page.getByRole('button', { name: '校验参数', exact: true }).click();
    await expect(page.getByRole('status')).toContainText('未调用外部服务');
    await page.getByLabel('显示名称', { exact: true }).fill('公开查询二版');
    await page.getByRole('button', { name: '发布新版本', exact: true }).click();
    await expect(page.getByRole('status')).toContainText('v2');
    await page.screenshot({ path: path.join(output, 'tools-management-desktop.png') });
    await page.getByRole('button', { name: 'Agent', exact: true }).click();
    await expect(page.getByLabel('公开查询二版（待配置）', { exact: true })).toBeVisible();
    await page.getByLabel('公开查询二版（待配置）', { exact: true }).check();
    await page.getByRole('button', { name: '创建助手', exact: true }).click();
    await expect(page.getByRole('status')).toContainText('版本发布成功');
    await page.locator('.agent-list').getByRole('button', { name: /通用助手/ }).click();
    await page.getByRole('button', { name: '删除助手', exact: true }).click();
    const confirmation = page.getByRole('dialog', { name: '删除助手', exact: true });
    await expect(confirmation).toBeVisible();
    await expect(confirmation.getByRole('button', { name: '取消', exact: true })).toBeFocused();
    const desktopBox = await confirmation.boundingBox();
    expect(Math.abs(desktopBox.x + desktopBox.width / 2 - 1366 / 2)).toBeLessThan(2);
    expect(Math.abs(desktopBox.y + desktopBox.height / 2 - 1000 / 2)).toBeLessThan(2);
    await page.screenshot({ path: path.join(output, 'agent-delete-confirm-desktop.png') });
    await page.keyboard.press('Escape');
    await expect(confirmation).toHaveCount(0);
    await expect(page.getByRole('button', { name: '删除助手', exact: true })).toBeFocused();
    await expect(page.locator('.agent-list')).toContainText('通用助手');
    await page.getByRole('button', { name: '切换到深色主题', exact: true }).click();
    await page.getByRole('button', { name: '删除助手', exact: true }).click();
    await page.screenshot({ path: path.join(output, 'agent-delete-confirm-dark.png') });
    await confirmation.getByRole('button', { name: '取消', exact: true }).click();
    await expect(confirmation).toHaveCount(0);
    await page.getByRole('button', { name: '切换到浅色主题', exact: true }).click();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole('button', { name: '删除助手', exact: true }).click();
    const mobileBox = await confirmation.boundingBox();
    expect(mobileBox.x).toBeGreaterThanOrEqual(16);
    expect(mobileBox.x + mobileBox.width).toBeLessThanOrEqual(374);
    await page.screenshot({ path: path.join(output, 'agent-delete-confirm-mobile.png') });
    await confirmation.getByRole('button', { name: '取消', exact: true }).click();
    await expect(confirmation).toHaveCount(0);
    await page.setViewportSize({ width: 1366, height: 1000 });
    await page.getByRole('button', { name: '删除助手', exact: true }).click();
    await confirmation.getByRole('button', { name: '确认删除', exact: true }).click();
    await expect(page.getByRole('status')).toContainText('助手已删除');
    await expect(page.locator('.agent-list')).not.toContainText('通用助手');
    await expect(page.getByRole('button', { name: '删除助手', exact: true })).toHaveCount(0);
    await page.reload();
    await page.getByRole('button', { name: 'Agent', exact: true }).click();
    await expect(page.locator('.agent-list')).not.toContainText('通用助手');
    await page.getByRole('button', { name: 'Skill', exact: true }).click();
    await page.getByLabel('Skill 名称', { exact: true }).fill('query-skill');
    await page.getByLabel('技能描述', { exact: true }).fill('按授权工具查询');
    await page.getByLabel('公开查询二版（待配置）', { exact: true }).check();
    await page.getByRole('button', { name: '创建 Skill', exact: true }).click();
    await expect(page.getByRole('status')).toContainText('v1');
    await page.getByRole('button', { name: 'Tool', exact: true }).click();
    await page.getByRole('button', { name: /公开查询二版.*v2/ }).click();
    await page.getByRole('button', { name: '停用 Tool', exact: true }).click();
    await expect(page.getByRole('status')).toContainText('已停用');
    await page.getByRole('button', { name: '启用 Tool', exact: true }).click();
    await expect(page.getByRole('status')).toContainText('已启用');
    await page.setViewportSize({ width: 390, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: path.join(output, 'tools-management-mobile.png') });
    await page.getByRole('button', { name: '删除 Tool', exact: true }).click();
    await expect(page.getByRole('status')).toContainText('已删除');
    await expect(page.locator('.agent-list')).not.toContainText('公开查询二版');
    expect(errors).toEqual([]);
    expect(external).toEqual([]);
    fs.writeFileSync(path.join(output, 'tools-ui-validation.json'), JSON.stringify({
      passed: true, api: 'real FastAPI', database: 'isolated in-memory SQLite',
      paid_model_requests: 0, external_tool_requests: 0, user_business_data_touched: false,
      checks: ['默认五项', '网页搜索目录', '天气路况待配置', '内置不可编辑删除', '启停', '创建与 v2', '本地参数校验', 'Agent 授权目录', '居中确认框深浅主题与窄屏', 'Esc 与焦点返回', '助手删除确认与取消', '删除后刷新隐藏', 'Skill 授权目录', '390px 布局', '删除后隐藏'],
      browser_errors: errors,
    }, null, 2));
    console.log('Tool 浏览器验收通过，真实 API，零外部查询。');
  } catch (error) {
    console.error(log.slice(-2000));
    throw error;
  } finally {
    if (browser) await browser.close();
    child.kill();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });

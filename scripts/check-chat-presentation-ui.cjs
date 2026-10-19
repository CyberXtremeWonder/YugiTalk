const { chromium, expect } = require('../frontend/node_modules/@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const streamError = process.argv.includes('--stream-error');

/** 用只读接口替身验收真实开发页面，避免调用付费模型或改写业务数据。 */
async function main() {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const errors = [];
    let writes = 0;
    let previews = 0;
    page.on('pageerror', error => errors.push(error.message));
    const source = {
      chunk_id: 'fixture-chunk', document_id: 'fixture-doc', label: 'S2',
      filename: '架构说明.md', paragraph: 2, evidence_id: 'E-fixture', index_version: 1,
    };
    await page.route('**/api/v1/**', async route => {
      const request = route.request();
      if (request.method() !== 'GET') { writes++; await route.abort(); return; }
      const pathname = new URL(request.url()).pathname;
      let json = { items: [], next_cursor: null };
      if (pathname.endsWith('/auth/me')) json = {
        user: { id: 'fixture', name: '显示验收', email: 'fixture@example.test', platform_admin: false },
        workspaces: [{ id: 'fixture-space', name: '只读验收', role: 'owner' }], csrf_token: 'fixture',
      };
      else if (pathname.endsWith('/conversations')) json.items = [{ id: 'fixture-chat', title: '知识库显示验收' }];
      else if (pathname.endsWith('/messages')) json.items = [
        { id: 'fixture-question', role: 'user', content: '解释正文 [S2] 是什么', sources: [] },
        { id: 'fixture-answer', role: 'assistant', content: '## 架构说明\n\n向量保存到 Milvus。[S2][S3]\n\n元数据保存到 PostgreSQL。[ S12 ]', sources: [source] },
      ];
      else if (pathname.endsWith('/sources/fixture-chunk')) { previews++; json = { ...source, content: '受控原文：Milvus 保存向量，PostgreSQL 保存元数据。' }; }
      else if (streamError && pathname.endsWith('/runs')) json.items = [{
        id: 'fixture-run', status: 'failed', error_code: 'model_stream_size_limit',
        provider: 'openai_compatible', model: 'fixture', charged_tokens: 48331,
        input_tokens: null, output_tokens: null, events_url: '/api/v1/runs/fixture-run/events', event_sequence: 1,
      }];
      else if (streamError && pathname.endsWith('/events')) {
        await route.fulfill({ contentType: 'text/event-stream', body: 'id: fixture-run:1\ndata: ' + JSON.stringify({ sequence: 1, type: 'error', payload: { status: 'failed', error_code: 'model_stream_size_limit' } }) + '\n\n' });
        return;
      }
      await route.fulfill({ json });
    });
    await page.goto('http://127.0.0.1:8080/');
    await page.getByRole('button', { name: '知识库显示验收', exact: true }).click();
    const assistant = page.locator('.message.assistant');
    await expect(assistant).toContainText('向量保存到 Milvus。');
    await expect(assistant).not.toContainText('[S2]');
    await expect(assistant).not.toContainText('[S3]');
    await expect(assistant).not.toContainText('[ S12 ]');
    if (streamError) await expect(page.getByText(/模型响应流超过大小保护上限/)).toBeVisible();
    await expect(page.locator('.message.user')).toContainText('[S2]');
    await page.getByText('检索来源（1）', { exact: true }).click();
    await page.getByRole('button', { name: '架构说明.md · 第 2 段' }).click();
    await expect(page.locator('.source-preview')).toContainText('受控原文');
    expect(previews).toBe(1);
    await page.reload();
    await page.getByRole('button', { name: '知识库显示验收', exact: true }).click();
    await expect(page.locator('.message.assistant')).not.toContainText('[S2]');
    expect(writes).toBe(0);
    expect(errors).toEqual([]);
    const prefix = streamError ? 'stream-size' : 'chat-presentation';
    await page.screenshot({ path: path.resolve(__dirname, `../docs/stages/artifacts/${prefix}-browser.png`), fullPage: true });
    fs.writeFileSync(path.resolve(__dirname, `../docs/stages/artifacts/${prefix}-ui-validation.json`), JSON.stringify({
      passed: true, hidden_history_labels: true, user_text_preserved: true,
      controlled_source_preview: true, source_requests: previews, reload_passed: true,
      write_requests: writes, browser_errors: errors, api: 'read-only fixtures',
      frontend: 'actual development Docker image', paid_model_requests: 0,
      stream_error_hint_verified: streamError,
    }, null, 2));
    console.log('历史正文隐藏索引、来源卡片、原文预览和刷新验收通过。');
  } finally { await browser.close(); }
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });

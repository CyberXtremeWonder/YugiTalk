// 隔离的记忆等级表单验收：仅使用路由桩，不访问或修改真实记忆。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "../frontend/node_modules/vite/dist/node/index.js";
import { chromium } from "../frontend/node_modules/playwright/index.mjs";

const frontend = fileURLToPath(new URL("../frontend", import.meta.url));
const directory = resolve(frontend, ".cache/memory-ranking-ui");
await mkdir(directory, { recursive: true });
const server = await createServer({
  root: frontend,
  configFile: false,
  plugins: [{ name: "memory-test-page", configureServer: installTestPage }],
  server: { host: "127.0.0.1", port: 15186, strictPort: true },
});
function installTestPage(server) {
  server.middlewares.use((req, res, next) => {
  if (req.url !== "/__memory-test") return next();
  res.setHeader("Content-Type", "text/html; charset=utf-8");
  res.end(`<!doctype html><html lang="zh-CN"><head><meta name="viewport" content="width=device-width,initial-scale=1"></head>
    <body><main id="main-panel"></main><script type="module">
      import React from '/node_modules/.vite/deps/react.js';
      import ReactDOM from '/node_modules/.vite/deps/react-dom_client.js';
      import { MemorySettings } from '/src/memory/MemorySettings.tsx';
      import '/src/shared/styles.css';
      ReactDOM.createRoot(document.getElementById('main-panel')).render(React.createElement(MemorySettings, {
        identity: {user: {id:'test',name:'测试用户',email:'test@example.test',platform_admin:false},csrf_token:'test-csrf'},
        accountId:'test'
      }));
    </script></body></html>`);
  });
}
let browser;
try {
  await server.listen();
  browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
  page.setDefaultTimeout(10000);
  const errors = [], writes = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => {
    if (message.type() === "error") console.error(message.text());
  });
  let rows = [], enabled = true;
  await page.route("**/api/v1/**", async route => {
    const request = route.request();
    const method = request.method();
    if (method !== "GET") {
      assert.equal(request.headers()["x-csrf-token"], "test-csrf");
      const body = request.postDataJSON();
      writes.push({ method, body });
      if (request.url().endsWith("memory-preference")) enabled = body.enabled;
      else if (method === "POST") rows.push({ id: "memory-test", ...body });
      else if (method === "PATCH") rows[0] = { ...rows[0], ...body };
      return route.fulfill({ json: rows[0] || {} });
    }
    return route.fulfill({ json: { enabled, items: enabled ? rows : [] } });
  });
  await page.goto("http://127.0.0.1:15186/__memory-test");
  const level = page.getByLabel("重要程度", { exact: true });
  try {
    await level.waitFor();
  } catch (error) {
    console.error(JSON.stringify({ body: await page.locator("body").innerText(), errors }));
    throw error;
  }
  assert.equal(await level.inputValue(), "3");
  await page.getByLabel("偏好内容", { exact: true }).fill("默认用中文回答");
  await level.selectOption("5");
  await page.getByRole("checkbox", { name: "我确认保存此偏好" }).check();
  await page.getByRole("button", { name: "保存偏好", exact: true }).click();
  await page.getByText("重要程度：5 / 5", { exact: true }).waitFor();
  assert.equal(writes[0].body.importance, 5);
  assert.equal(await level.inputValue(), "3");
  await page.getByRole("button", { name: "修改", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.waitFor();
  assert.equal(await dialog.getByLabel("重要程度", { exact: true }).inputValue(), "5");
  await dialog.getByLabel("重要程度", { exact: true }).selectOption("2");
  await dialog.getByRole("checkbox", { name: "我确认保存此偏好" }).check();
  await dialog.getByRole("button", { name: "更新偏好", exact: true }).click();
  await page.getByText("重要程度：2 / 5", { exact: true }).waitFor();
  assert.equal(writes[1].body.importance, 2);
  await page.setViewportSize({ width: 400, height: 850 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.screenshot({ path: resolve(directory, "memory-mobile.png"), fullPage: true });
  await page.getByRole("checkbox", { name: "启用长期记忆" }).uncheck();
  await page.getByText("偏好已更新。", { exact: true }).waitFor();
  assert.equal(await level.count(), 0);
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, writes, pageErrors: errors, screenshots: directory }));
} finally {
  await browser?.close();
  await server.close();
}

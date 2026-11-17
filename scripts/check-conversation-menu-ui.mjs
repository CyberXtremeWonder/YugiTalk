// 用隔离 API 替身验证完整工作台，不读取或修改真实对话。
import assert from "node:assert/strict";
import { mkdir, writeFile, readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";
import { createServer } from "../frontend/node_modules/vite/dist/node/index.js";
import { chromium } from "../frontend/node_modules/playwright/index.mjs";

const frontend = fileURLToPath(new URL("../frontend", import.meta.url));
const directory = resolve(frontend, ".cache/conversation-menu-ui");
const downloads = [];
await mkdir(directory, { recursive: true });
const server = await createServer({
  root: frontend, configFile: false,
  plugins: [{
    name: "isolated-conversation-downloads",
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        const url = new URL(request.url, "http://localhost");
        if (!url.pathname.endsWith("/export")) return next();
        const format = url.searchParams.get("format");
        downloads.push(format);
        response.setHeader("Content-Type", "application/octet-stream");
        response.setHeader("Content-Disposition", `attachment; filename=conversation.${format}`);
        response.end("synthetic conversation");
      });
    },
  }],
  server: { host: "127.0.0.1", port: 15176, strictPort: true },
});
let browser;
try {
  await server.listen();
  browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await browser.newPage({ viewport: { width: 1200, height: 850 } });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  let rows = [
    { id: "recent", title: "最近更新的对话", updated_at: "2026-11-02T05:00:00Z" },
    { id: "older", title: "较早的对话", updated_at: "2026-11-01T05:00:00Z" },
  ];
  await page.route("**/api/v1/**", async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace("/api/v1", "");
    let json = { items: [], next_cursor: null };
    if (path === "/auth/me") json = {
      user: { id: "user", name: "测试用户", email: "test@example.test", platform_admin: false },
      csrf_token: "test",
    };
    if (path === "/me/conversations") json = { items: rows, next_cursor: null };
    if (path.endsWith("/export")) {
      downloads.push(url.searchParams.get("format"));
      return route.fulfill({ body: "synthetic conversation", headers: {
        "content-type": "application/octet-stream",
        "content-disposition": `attachment; filename=conversation.${url.searchParams.get("format")}`,
      } });
    }
    if (request.method() === "PATCH") {
      assert.equal(request.headers()["x-csrf-token"], "test");
      const id = path.split("/").at(-1);
      rows = rows.map(row => row.id === id ? { ...row, title: request.postDataJSON().title } : row);
      json = rows.find(row => row.id === id);
    }
    if (request.method() === "DELETE") {
      assert.equal(request.headers()["x-csrf-token"], "test");
      rows = rows.filter(row => row.id !== path.split("/").at(-1));
      return route.fulfill({ status: 204 });
    }
    return route.fulfill({ json });
  });
  await page.goto("http://127.0.0.1:15176/");
  const history = page.locator(".history");
  await history.getByRole("button", { name: "最近更新的对话", exact: true }).waitFor();
  assert.deepEqual(await history.locator(".history-item > button:first-child").allTextContents(),
    ["最近更新的对话", "较早的对话"]);
  assert.equal(await page.getByText("管理对话", { exact: true }).count(), 0);
  const trigger = () => page.getByRole("button", { name: "对话选项：最近更新的对话", exact: true });
  await trigger().click();
  await page.getByRole("button", { name: "重命名", exact: true }).waitFor();
  await page.screenshot({ path: resolve(directory, "menu-light.png"), fullPage: true });
  await page.keyboard.press("Escape");
  assert.equal(await page.getByRole("button", { name: "重命名", exact: true }).isVisible(), false);
  await trigger().click();
  await page.getByRole("heading", { name: "最近对话" }).click();
  assert.equal(await page.getByRole("button", { name: "重命名", exact: true }).isVisible(), false);
  for (const format of ["pdf", "docx", "txt", "json"]) {
    await trigger().click();
    await page.getByRole("button", { name: "导出对话", exact: true }).click();
    assert.deepEqual(await page.locator(".conversation-menu:popover-open a").allTextContents(),
      ["PDF", "Word", "TXT", "JSON"]);
    const download = page.waitForEvent("download");
    await page.locator(`.conversation-menu:popover-open a[href$='format=${format}']`).click();
    const result = await download;
    assert.equal(result.suggestedFilename(), `conversation.${format}`);
    assert.equal(await readFile(await result.path(), "utf8"), "synthetic conversation");
  }
  assert.deepEqual(downloads, ["pdf", "docx", "txt", "json"]);
  await trigger().click();
  await page.getByRole("button", { name: "重命名", exact: true }).click();
  await page.getByRole("dialog").waitFor();
  await page.getByRole("textbox", { name: "重命名 最近更新的对话" }).fill("已修改的对话名称");
  await page.getByRole("button", { name: "保存名称", exact: true }).click();
  await history.getByRole("button", { name: "已修改的对话名称", exact: true }).waitFor();
  await history.getByRole("button", { name: "已修改的对话名称", exact: true }).click();
  await page.getByRole("button", { name: "切换到深色主题" }).click();
  await page.getByRole("button", { name: "对话选项：已修改的对话名称" }).click();
  const menu = page.locator(".conversation-menu:popover-open");
  const background = await menu.evaluate(el => getComputedStyle(el).backgroundColor);
  assert.notEqual(background, "rgb(255, 255, 255)");
  await page.screenshot({ path: resolve(directory, "menu-dark.png"), fullPage: true });
  await page.getByRole("button", { name: "删除对话", exact: true }).click();
  assert.equal(await history.getByRole("button", { name: "已修改的对话名称", exact: true }).count(), 0);
  await page.setViewportSize({ width: 400, height: 850 });
  const mobileMenu = page.locator("#menu");
  if (await mobileMenu.count()) await mobileMenu.first().click();
  await page.getByRole("button", { name: "对话选项：较早的对话" }).click();
  await page.getByRole("button", { name: "导出对话", exact: true }).click();
  const mobileBounds = await page.locator(".conversation-menu:popover-open").boundingBox();
  assert.ok(mobileBounds.x >= 0 && mobileBounds.x + mobileBounds.width <= 400);
  assert.ok(mobileBounds.y + mobileBounds.height <= 850);
  await page.screenshot({ path: resolve(directory, "menu-mobile.png"), fullPage: true });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, downloads, screenshots: directory, pageErrors: errors }));
} finally {
  await browser?.close();
  await server.close();
}

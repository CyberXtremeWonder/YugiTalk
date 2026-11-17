// 使用隔离 API 数据验证文档管理，不访问真实数据库、模型或文件存储。
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "../frontend/node_modules/vite/dist/node/index.js";
import { chromium } from "../frontend/node_modules/playwright/index.mjs";

const frontend = fileURLToPath(new URL("../frontend", import.meta.url));
const directory = resolve(frontend, ".cache/knowledge-library-ui");
await mkdir(directory, { recursive: true });
const server = process.env.UI_BASE_URL
  ? null
  : await createServer({
      root: frontend,
      configFile: false,
      server: { host: "127.0.0.1", port: 15178, strictPort: true },
    });
let browser;
try {
  await server?.listen();
  browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await browser.newPage({
    viewport: { width: 1600, height: 1050 },
    timezoneId: "Asia/Shanghai",
  });
  const errors = [],
    uploads = [],
    retries = [],
    searches = [],
    deletes = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let bases = [{ id: "base", name: "Agent 相关论文" }];
  let docs = [
    ["plan", "Plan-and-Solve.pdf", "ready", 3617 * 1024, "pc-short-short-v1"],
    ["react", "ReAct.pdf", "ready", 2867 * 1024, "pc-auto-structured-v1"],
    [
      "notes",
      "Agent 架构笔记.docx",
      "ready",
      426 * 1024,
      "pc-structured-structured-v1",
    ],
    [
      "sheet",
      "论文实验数据.xlsx",
      "ready",
      1229 * 1024,
      "pc-auto-structured-v1",
    ],
    ["review", "多智能体综述.pdf", "indexing", 5222 * 1024, "pc-auto-long-v1"],
    ["excerpt", "研究摘录.txt", "failed", 82 * 1024, "pc-auto-short-v1"],
  ].map(([id, filename, status, size, chunk_strategy]) => ({
    id,
    filename,
    status,
    size,
    chunk_strategy,
    index_version: 1,
    created_at: "2026-11-02T06:20:00Z",
    error_code: status === "failed" ? "embedding_timeout" : null,
    progress:
      status === "indexing"
        ? {
            stage: "writing",
            total_chunks: 200,
            embedded_chunks: 200,
            indexed_chunks: 136,
          }
        : {},
  }));
  await page.route("**/api/v1/**", (route) => {
    const request = route.request(),
      url = new URL(request.url()),
      path = url.pathname;
    let json = { items: [], next_cursor: null },
      status = 200;
    if (request.method() !== "GET")
      assert.equal(request.headers()["x-csrf-token"], "test");
    if (path.endsWith("/auth/me"))
      json = {
        user: { id: "user", name: "测试用户", platform_admin: false },
        csrf_token: "test",
      };
    if (path.endsWith("/me/knowledge-bases")) {
      if (request.method() === "POST") {
        const row = { id: "new", name: request.postDataJSON().name };
        bases.push(row);
        json = row;
        status = 201;
      } else json = { items: bases, next_cursor: null };
    }
    if (/knowledge-bases\/[^/]+\/documents$/.test(path)) {
      if (request.method() === "POST") {
        uploads.push({
          filename: url.searchParams.get("filename"),
          strategy: url.searchParams.get("chunk_strategy"),
        });
        json = {
          ...docs[0],
          id: `upload-${uploads.length}`,
          filename: url.searchParams.get("filename"),
          status: "ready",
        };
        docs.push(json);
        status = 202;
      } else
        json = {
          items: path.includes("/base/") ? docs : [],
          next_cursor: null,
        };
    }
    if (path.endsWith("/retry")) {
      retries.push(url.searchParams.get("chunk_strategy"));
      json = docs.find((doc) => path.includes(`/${doc.id}/`));
    }
    if (/\/documents\/[^/]+$/.test(path) && request.method() === "DELETE") {
      const id = path.split("/").at(-1);
      deletes.push(id);
      json = { ...docs.find((doc) => doc.id === id), status: "deleted" };
      docs = docs.filter((doc) => doc.id !== id);
    }
    if (path.endsWith("/search")) {
      searches.push(request.postDataJSON().query);
      json = { items: [] };
    }
    if (
      path.endsWith("/knowledge-bases/new") &&
      request.method() === "DELETE"
    ) {
      bases = bases.filter((base) => base.id !== "new");
      return route.fulfill({ status: 204 });
    }
    return route.fulfill({ status, json });
  });
  await page.goto(process.env.UI_BASE_URL || "http://127.0.0.1:15178/");
  await page.getByRole("button", { name: "知识库", exact: true }).click();
  await page
    .getByRole("button", { name: "Plan-and-Solve.pdf", exact: true })
    .waitFor();
  const rows = () =>
    page.locator(".km-file-table tbody > tr:not(.km-detail-row)");
  assert.equal(await rows().count(), 6);
  assert.equal(
    await page
      .getByRole("textbox", { name: "知识库名称", exact: true })
      .isVisible(),
    true,
  );
  assert.equal(
    await page.getByRole("button", { name: "构建图谱", exact: true }).count(),
    0,
  );
  await page.screenshot({
    path: resolve(directory, "library-light.png"),
    fullPage: true,
    animations: "disabled",
  });
  await page.getByRole("button", { name: "可用 4", exact: true }).click();
  assert.equal(await rows().count(), 4);
  await page.getByRole("button", { name: "失败 1", exact: true }).click();
  assert.equal(await rows().count(), 1);
  await page.getByRole("button", { name: "全部 6", exact: true }).click();
  await page.getByLabel("搜索文件名称").fill("plan");
  assert.equal(await rows().count(), 1);
  await page.getByLabel("搜索文件名称").fill("no-match");
  await page.getByText("没有匹配的文件", { exact: true }).waitFor();
  await page.getByLabel("搜索文件名称").fill("");
  await page
    .getByRole("button", { name: "文件选项：Plan-and-Solve.pdf" })
    .click();
  await page.getByLabel("分块类型：Plan-and-Solve.pdf").selectOption("long");
  await page.getByRole("button", { name: "重新索引", exact: true }).click();
  assert.deepEqual(retries, ["long"]);
  await page.getByRole("button", { name: "文件选项：研究摘录.txt" }).click();
  await page
    .locator(".library-action-menu:popover-open")
    .getByRole("button", { name: "处理详情", exact: true })
    .click();
  await page
    .locator(".km-detail-row")
    .getByText(/Embedding 请求超时/)
    .waitFor();
  await page
    .getByRole("button", { name: "删除文件：研究摘录.txt", exact: true })
    .click();
  assert.deepEqual(deletes, ["excerpt"]);
  assert.equal(await rows().count(), 5);
  await page
    .getByLabel("文档分块类型", { exact: true })
    .selectOption("structured");
  await page
    .getByLabel("上传资料（JSONL 最多 5 GB，其他文件最多 20 MB）")
    .setInputFiles({
      name: "测试上传.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("上传测试"),
    });
  await page
    .getByRole("button", { name: "测试上传.txt", exact: true })
    .waitFor();
  assert.deepEqual(uploads[0], {
    filename: "测试上传.txt",
    strategy: "structured",
  });
  const transfer = await page.evaluateHandle(() => {
    const transfer = new DataTransfer();
    transfer.items.add(
      new File(["拖放内容"], "拖放测试.txt", { type: "text/plain" }),
    );
    return transfer;
  });
  await page
    .locator(".km-dropzone")
    .dispatchEvent("drop", { dataTransfer: transfer });
  await page
    .getByRole("button", { name: "拖放测试.txt", exact: true })
    .waitFor();
  const folder = resolve(directory, "folder-fixture");
  await mkdir(resolve(folder, "nested"), { recursive: true });
  await writeFile(resolve(folder, "文件夹资料一.txt"), "第一份文件夹资料");
  await writeFile(
    resolve(folder, "nested", "文件夹资料二.txt"),
    "第二份文件夹资料",
  );
  await page.getByLabel("上传文件夹", { exact: true }).setInputFiles(folder);
  await page
    .getByRole("button", { name: "文件夹资料二.txt", exact: true })
    .waitFor();
  assert.equal(uploads.length, 4);
  await page.getByLabel("检索问题").fill("如何减少推理错误？");
  await page.getByRole("button", { name: "检索资料", exact: true }).click();
  assert.deepEqual(searches, ["如何减少推理错误？"]);
  await page.getByRole("button", { name: "切换到深色主题" }).click();
  await page.locator(".knowledge-manager").evaluate((el) => {
    el.scrollTop = 0;
  });
  await page.screenshot({
    path: resolve(directory, "library-dark.png"),
    fullPage: true,
    animations: "disabled",
  });
  await page.getByLabel("知识库名称", { exact: true }).fill("新增测试库");
  await page.getByRole("button", { name: "创建知识库", exact: true }).click();
  await page.getByText("知识库已创建。", { exact: true }).waitFor();
  assert.equal(
    await page.getByLabel("当前知识库", { exact: true }).inputValue(),
    "new",
  );
  await page.getByRole("button", { name: "删除知识库", exact: true }).click();
  await page.getByRole("dialog").waitFor();
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await page.getByRole("button", { name: "删除知识库", exact: true }).click();
  await page.getByRole("button", { name: "确认删除", exact: true }).click();
  await page.getByText("知识库已删除。", { exact: true }).waitFor();
  await page.setViewportSize({ width: 400, height: 850 });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
    true,
  );
  assert.equal(
    await page
      .locator(".knowledge-manager")
      .evaluate((el) => el.scrollWidth <= el.clientWidth),
    true,
  );
  await page.screenshot({
    path: resolve(directory, "library-mobile.png"),
    fullPage: true,
  });
  assert.deepEqual(errors, []);
  console.log(
    JSON.stringify({
      passed: true,
      target: process.env.UI_BASE_URL || "http://127.0.0.1:15178",
      checks: [
        "table",
        "search",
        "status-filter",
        "reindex-profile",
        "failure-detail",
        "file-delete",
        "upload",
        "folder-upload",
        "drag-drop",
        "retrieval",
        "inline-create",
        "delete-confirm",
        "dark",
        "mobile",
      ],
      screenshots: directory,
    }),
  );
} finally {
  await browser?.close();
  await server?.close();
}

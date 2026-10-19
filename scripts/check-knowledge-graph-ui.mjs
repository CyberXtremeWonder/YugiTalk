import { mkdir, writeFile, rm } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";
import { createServer } from "../frontend/node_modules/vite/dist/node/index.js";
import { chromium } from "../frontend/node_modules/playwright/index.mjs";

const root = fileURLToPath(new URL("../", import.meta.url));
const frontend = resolve(root, "frontend");
const harness = resolve(frontend, ".cache/graph-ui");
const artifacts = resolve(root, "docs/stages/artifacts");
await mkdir(harness, { recursive: true });
await mkdir(artifacts, { recursive: true });
await writeFile(
  resolve(harness, "index.html"),
  '<!doctype html><html lang="zh-CN"><meta charset="UTF-8"><title>图谱页面验收</title><div id="root"></div><script type="module" src="./main.tsx"></script></html>',
);
await writeFile(
  resolve(harness, "main.tsx"),
  `import { createRoot } from 'react-dom/client';
import { GraphPanel } from '../../src/rag/GraphPanel';
import '../../src/shared/styles.css';
import '../../src/shared/hermes.css';
createRoot(document.getElementById('root')!).render(<main><h1>知识图谱与原文依据</h1><GraphPanel documentId="graph-ui-test" csrf="test-csrf" /></main>);`,
);

const server = await createServer({
  root: frontend,
  server: { host: "127.0.0.1", port: 15173, strictPort: true },
});
let browser;
try {
  await server.listen();
  browser = await chromium.launch({
    channel: process.platform === "win32" ? "msedge" : undefined,
    headless: true,
  });
  const page = await browser.newPage({
    viewport: { width: 1000, height: 800 },
  });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let status = "unavailable";
  let submissions = 0;
  await page.route("**/api/v1/documents/graph-ui-test/graph", async (route) => {
    if (route.request().method() === "POST") {
      if (route.request().headers()["x-csrf-token"] !== "test-csrf")
        throw new Error("缺少防伪令牌");
      submissions += 1;
      status = "ready";
      await route.fulfill({ json: { status: "queued" } });
    } else {
      await route.fulfill({
        json: {
          status,
          mode: "model",
          entity_count: status === "ready" ? 2 : undefined,
          facts:
            status === "ready"
              ? [
                  {
                    source: "支付服务",
                    target: "DB1",
                    relation: "使用",
                    quote: "支付服务使用DB1。",
                    chunk_id: "test-chunk",
                  },
                ]
              : [],
        },
      });
    }
  });
  await page.goto("http://127.0.0.1:15173/.cache/graph-ui/index.html");
  await page.getByText("知识图谱与原文依据", { exact: true }).last().click();
  await page.getByText("图谱：尚未构建").waitFor();
  await page.getByRole("button", { name: "构建图谱", exact: true }).click();
  await page.getByText("支付服务使用DB1。", { exact: true }).waitFor();
  await page.getByRole("button", { name: "重新构建图谱" }).waitFor();
  if (submissions !== 1 || errors.length)
    throw new Error(JSON.stringify({ submissions, errors }));
  await page.screenshot({
    path: resolve(artifacts, "knowledge-graph-ui.png"),
    fullPage: true,
  });
  await writeFile(
    resolve(artifacts, "knowledge-graph-ui.json"),
    JSON.stringify(
      {
        passed: true,
        submissions,
        errors,
        checks: [
          "展开与状态加载",
          "补建图谱与防伪令牌",
          "完成状态刷新",
          "关系与原文显示",
          "重新构建入口",
        ],
        scope: "真实前端组件与浏览器，接口替身；未连接业务服务或调用真实模型",
      },
      null,
      2,
    ),
  );
  console.log("图谱页面验证通过");
} finally {
  await browser?.close();
  await server.close();
  await rm(resolve(harness, "index.html"), { force: true });
  await rm(resolve(harness, "main.tsx"), { force: true });
}

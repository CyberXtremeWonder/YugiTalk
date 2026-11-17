// 任务工作台验收；拦截全部 API，仅使用示例数据。UI_BASE_URL 可指向容器。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";
import { createServer } from "../frontend/node_modules/vite/dist/node/index.js";
import { chromium } from "../frontend/node_modules/playwright/index.mjs";
const frontend = fileURLToPath(new URL("../frontend", import.meta.url));
const directory = resolve(frontend, ".cache/task-history-ui");
await mkdir(directory, { recursive: true });
const server = process.env.UI_BASE_URL
  ? null
  : await createServer({
      root: frontend,
      configFile: false,
      server: { host: "127.0.0.1", port: 15175, strictPort: true },
    });
const origin = process.env.UI_BASE_URL || "http://127.0.0.1:15175";
let browser;
try {
  await server?.listen();
  browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await browser.newPage({
    viewport: { width: 1600, height: 1100 },
    timezoneId: "Asia/Shanghai",
  });
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  const errors = [],
    writes = [],
    opened = [],
    cursors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const defaults = {
    provider: "openai_compatible",
    model: "Qwen3-32B",
    charged_tokens: 8360,
    input_tokens: 6240,
    output_tokens: 1280,
    attempts: 1,
    error_code: null,
    retryable: false,
  };
  let runs = [
    {
      id: "completed",
      conversation_id: "weekly",
      conversation_title: "周报自动整理",
      prompt_preview: "汇总本周进展与下周计划",
      status: "succeeded",
      created_at: "2026-11-02T11:42:00Z",
    },
    {
      id: "failed",
      conversation_id: "papers",
      conversation_title: "Agent 论文对比分析",
      prompt_preview: "对比这两篇论文的核心方法、实验结果与适用场景。",
      status: "failed",
      error_code: "model_transient",
      retryable: true,
      created_at: "2026-11-02T12:05:00Z",
      attempts: 2,
    },
    {
      id: "running",
      conversation_id: "knowledge",
      conversation_title: "知识库文档检索",
      prompt_preview: "整理 Agent 论文中的关键结论",
      status: "running",
      created_at: "2026-11-02T12:18:00Z",
    },
    {
      id: "waiting",
      conversation_id: "upload",
      conversation_title: "上传资料问答",
      prompt_preview: "这份资料对应哪个知识库？",
      status: "waiting_input",
      clarification: {
        prompt: "请选择要使用的知识库。",
        options: ["Agent 论文", "工作资料"],
      },
      input_tokens: null,
      output_tokens: null,
      created_at: "2026-11-02T11:28:00Z",
    },
    {
      id: "graph",
      conversation_id: "graph-chat",
      conversation_title: "知识图谱关系梳理",
      prompt_preview: "查看实体之间的主要关联",
      status: "succeeded",
      created_at: "2026-11-02T10:55:00Z",
    },
    {
      id: "finance",
      conversation_id: "finance-chat",
      conversation_title: "财务报销制度",
      prompt_preview: "差旅住宿报销上限是多少？",
      status: "succeeded",
      charged_tokens: 0,
      input_tokens: 0,
      output_tokens: 0,
      created_at: "2026-11-02T10:21:00Z",
    },
    {
      id: "uncertain",
      conversation_id: "uncertain-chat",
      conversation_title: "MCP 远端调用",
      prompt_preview: "核查远端操作记录",
      status: "failed",
      error_code: "mcp_execution_uncertain",
      created_at: "2026-11-02T09:46:00Z",
    },
    {
      id: "stopped",
      conversation_id: "stopped-chat",
      conversation_title: "已停止的任务",
      prompt_preview: "停止后保留任务记录",
      status: "cancelled",
      error_code: "cancelled",
      created_at: "2026-11-02T09:30:00Z",
    },
    {
      id: "approval",
      conversation_id: "approval-chat",
      conversation_title: "待审批的任务",
      status: "waiting_approval",
      created_at: "2026-11-02T09:20:00Z",
    },
    {
      id: "queued",
      conversation_id: "queued-chat",
      conversation_title: "排队中的任务",
      status: "queued",
      created_at: "2026-11-02T09:10:00Z",
    },
  ].map((run) => ({
    ...defaults,
    ...run,
    events_url: `/api/v1/runs/${run.id}/events`,
  }));
  let failList = false,
    holdRetry = false,
    releaseRetry;
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request(),
      url = new URL(request.url()),
      path = url.pathname.replace("/api/v1", "");
    let json = { items: [], next_cursor: null };
    if (request.method() !== "GET") {
      assert.equal(request.headers()["x-csrf-token"], "test");
      writes.push({
        path,
        key: request.headers()["idempotency-key"],
        method: request.method(),
      });
    }
    if (path === "/auth/me")
      json = {
        user: { id: "user", name: "测试用户", platform_admin: false },
        csrf_token: "test",
      };
    if (path === "/me/agents")
      json = {
        items: [
          {
            id: "common",
            name: "通用助手",
            number: 1,
            version_id: "version",
            config: {
              provider: "mock",
              model: "mock-v1",
              tools: [],
              system_prompt: "测试助手",
            },
          },
        ],
        next_cursor: null,
      };
    if (path === "/me/conversations")
      json = {
        items: runs.map((run) => ({
          id: run.conversation_id,
          title: run.conversation_title,
        })),
        next_cursor: null,
      };
    if (path.startsWith("/conversations/")) opened.push(path.split("/")[2]);
    if (path === "/me/runs") {
      if (failList)
        return route.fulfill({
          status: 503,
          json: { detail: "test-unavailable" },
        });
      const cursor = url.searchParams.get("cursor");
      cursors.push(cursor);
      json = {
        items: cursor ? runs.slice(3) : runs.slice(0, 3),
        next_cursor: !cursor && runs.length > 3 ? "page-2" : null,
      };
    }
    if (path.endsWith("/retry")) {
      if (holdRetry) {
        await new Promise((resolve) => {
          releaseRetry = resolve;
        });
        holdRetry = false;
        return route.fulfill({
          status: 503,
          json: { detail: "retry-response-unknown" },
        });
      }
      json = { status: "queued" };
    }
    return route.fulfill({ json });
  });
  const nav = () =>
    page
      .locator("#navigation")
      .getByRole("button", { name: "任务记录", exact: true })
      .click();
  const select = (name) =>
    page
      .getByRole("button", { name: `查看任务：${name}`, exact: true })
      .click();
  const details = page.getByRole("complementary", {
    name: "任务详情",
    exact: true,
  });
  const filter = (label) =>
    page.getByRole("button", { name: new RegExp(`^${label}\\d+$`) }).click();
  await page.goto(origin);
  await nav();
  await page.locator(".th-table tbody tr").nth(9).waitFor();
  assert.ok(cursors.includes("page-2"));
  assert.equal(
    await page
      .locator(".th-table tbody tr")
      .first()
      .getByRole("button", { name: "查看任务：知识库文档检索", exact: true })
      .count(),
    1,
  );
  assert.deepEqual(await page.locator(".th-metric strong").allTextContents(), [
    "10",
    "2",
    "3",
    "4",
  ]);
  for (const [label, expected] of [
    ["运行中", 2],
    ["已完成", 3],
    ["失败", 2],
    ["待确认", 2],
    ["已停止", 1],
    ["全部", 10],
  ]) {
    await filter(label);
    assert.equal(await page.locator(".th-table tbody tr").count(), expected);
  }
  const search = page.getByLabel("搜索任务名称或内容", { exact: true });
  await search.fill("差旅住宿");
  assert.equal(await page.locator(".th-table tbody tr").count(), 1);
  assert.equal(
    await details
      .getByRole("button", { name: "恢复任务", exact: true })
      .count(),
    0,
  );
  assert.deepEqual(
    await details.locator(".th-tokens strong").allTextContents(),
    ["0", "0", "0"],
  );
  await search.fill("无匹配内容");
  await page.getByText("没有符合筛选条件的任务。", { exact: true }).waitFor();
  assert.equal(
    await details
      .getByRole("button", { name: "打开对话", exact: true })
      .count(),
    0,
  );
  await search.fill("");
  await select("上传资料问答");
  assert.equal(
    await details
      .getByText("请选择要使用的知识库。", { exact: true })
      .isVisible(),
    true,
  );
  assert.deepEqual(
    await details.locator(".th-tokens strong").allTextContents(),
    ["未提供", "未提供", "8,360"],
  );
  await select("MCP 远端调用");
  assert.equal(
    await details
      .getByRole("button", { name: "恢复任务", exact: true })
      .count(),
    0,
  );
  await select("Agent 论文对比分析");
  await page.screenshot({
    path: resolve(directory, "tasks-light.png"),
    fullPage: true,
    animations: "disabled",
  });
  await page.getByRole("button", { name: "切换到深色主题" }).click();
  await page.screenshot({
    path: resolve(directory, "tasks-dark.png"),
    fullPage: true,
    animations: "disabled",
  });
  await page.getByRole("button", { name: "切换到浅色主题" }).click();
  await page.getByRole("button", { name: "复制任务 ID", exact: true }).click();
  await page.getByText("任务 ID 已复制。", { exact: true }).waitFor();
  assert.equal(
    await page.evaluate(() => navigator.clipboard.readText()),
    "failed",
  );
  await details.getByRole("button", { name: "恢复任务", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "取消", exact: true })
    .click();
  assert.equal(writes.length, 0);
  await details.getByRole("button", { name: "恢复任务", exact: true }).click();
  holdRetry = true;
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "确认恢复任务", exact: true })
    .click();
  await page.getByRole("button", { name: "恢复中…", exact: true }).waitFor();
  assert.equal(
    await page
      .getByRole("dialog")
      .getByRole("button", { name: "取消", exact: true })
      .isDisabled(),
    true,
  );
  await page.keyboard.press("Escape");
  assert.equal(await page.getByRole("dialog").isVisible(), true);
  releaseRetry();
  await page.getByRole("dialog").getByRole("alert").waitFor();
  const firstKey = writes.at(-1).key;
  assert.ok(firstKey);
  assert.equal(writes.at(-1).path, "/runs/failed/retry");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "确认恢复任务", exact: true })
    .click();
  await page.waitForURL(/#chat$/);
  assert.equal(writes.at(-1).key, firstKey);
  await page.waitForFunction(() => location.hash === "#chat");
  assert.ok(opened.includes("papers"));
  await nav();
  await page.locator(".th-table tbody tr").nth(9).waitFor();
  await select("Agent 论文对比分析");
  await details.getByRole("button", { name: "恢复任务", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "确认恢复任务", exact: true })
    .click();
  await page.waitForURL(/#chat$/);
  assert.notEqual(writes.at(-1).key, firstKey);
  await nav();
  await page.locator(".th-table tbody tr").nth(9).waitFor();
  await select("财务报销制度");
  await details.getByRole("button", { name: "打开对话", exact: true }).click();
  await page.waitForURL(/#chat$/);
  assert.ok(opened.includes("finance-chat"));
  await nav();
  await page.locator(".th-table tbody tr").nth(9).waitFor();
  await select("Agent 论文对比分析");
  runs = runs.map((run) =>
    run.id === "failed"
      ? { ...run, status: "succeeded", error_code: null, retryable: false }
      : run,
  );
  await page.getByRole("button", { name: "刷新记录", exact: true }).click();
  await page.getByRole("button", { name: "刷新记录", exact: true }).waitFor();
  assert.equal(
    await details.locator(".th-detail-title strong").innerText(),
    "Agent 论文对比分析",
  );
  assert.equal(
    await details
      .getByRole("button", { name: "恢复任务", exact: true })
      .count(),
    0,
  );
  assert.deepEqual(await page.locator(".th-metric strong").allTextContents(), [
    "10",
    "2",
    "4",
    "3",
  ]);
  await page.setViewportSize({ width: 400, height: 850 });
  assert.equal(
    await page
      .locator(".task-workspace")
      .evaluate((el) => el.scrollWidth <= el.clientWidth),
    true,
  );
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
    true,
  );
  await page.screenshot({
    path: resolve(directory, "tasks-mobile.png"),
    fullPage: true,
    animations: "disabled",
  });
  await details
    .getByRole("button", { name: "打开对话", exact: true })
    .scrollIntoViewIfNeeded();
  assert.equal(
    await details
      .getByRole("button", { name: "打开对话", exact: true })
      .isVisible(),
    true,
  );
  await page.setViewportSize({ width: 1600, height: 1100 });
  failList = true;
  await page.getByRole("button", { name: "刷新记录", exact: true }).click();
  await page.getByRole("alert").waitFor();
  assert.equal(await page.locator(".th-table tbody tr").count(), 10);
  failList = false;
  runs = [];
  await page.getByRole("button", { name: "刷新记录", exact: true }).click();
  await page.getByText("尚无任务记录", { exact: true }).waitFor();
  assert.equal(await page.getByRole("alert").count(), 0);
  assert.equal(
    await details
      .getByRole("button", { name: "打开对话", exact: true })
      .count(),
    0,
  );
  failList = true;
  await page.getByRole("button", { name: "刷新记录", exact: true }).click();
  await page
    .getByText("任务记录加载失败，请刷新重试。", { exact: true })
    .waitFor();
  assert.deepEqual(errors, []);
  assert.ok(
    writes.every(
      (write) => write.method === "POST" && write.path === "/runs/failed/retry",
    ),
  );
  console.log(
    JSON.stringify({
      passed: true,
      target: origin,
      checks: [
        "pagination",
        "newest-first",
        "status-counts-filters",
        "content-search",
        "no-match",
        "null-versus-zero-tokens",
        "clarification",
        "retry-permission",
        "retry-cancel",
        "retry-busy",
        "retry-key-reuse",
        "retry-new-key-after-success",
        "open-conversation",
        "refresh-selection-status",
        "copy-id",
        "empty-error-recovery",
        "themes",
        "mobile",
      ],
      screenshots: directory,
    }),
  );
} finally {
  await browser?.close();
  await server?.close();
}

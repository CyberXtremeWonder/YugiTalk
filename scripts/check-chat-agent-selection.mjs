// 完整工作台的助手选择回归；所有 API 均由浏览器替身响应，不访问模型或真实对话。
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { createServer } from "../frontend/node_modules/vite/dist/node/index.js";
import { chromium } from "../frontend/node_modules/playwright/index.mjs";

const frontend = fileURLToPath(new URL("../frontend", import.meta.url));
const server = await createServer({
  root: frontend,
  configFile: false,
  server: { host: "127.0.0.1", port: 15203, strictPort: true },
});
const agent = (id, name) => ({
  id, name, version_id: "version-" + id, number: 1,
  config: {
    provider: "mock", model: "mock-v1", system_prompt: "", tools: [],
    token_budget: 10000, max_steps: 4, max_tool_calls: 4,
    max_output_tokens: 1000, timeout_seconds: 60,
  },
});
let agents = [agent("first", "默认助手"), agent("second", "保留选择的助手")];
const conversations = [], messages = [], runs = [], requests = [];
let browser;
try {
  await server.listen();
  browser = await chromium.launch({
    channel: process.platform === "win32" ? "msedge" : undefined,
    headless: true,
  });
  const page = await browser.newPage({ viewport: { width: 1200, height: 850 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/v1", "");
    let json;
    if (path === "/auth/me") json = {
      user: { id: "user", name: "测试用户", email: "test@example.test", platform_admin: false },
      csrf_token: "test",
    };
    else if (path === "/me/agents") json = { items: agents, next_cursor: null };
    else if (path === "/me/skills" || path === "/me/runs")
      json = { items: [], next_cursor: null };
    else if (path === "/accounts/user/memories") json = { enabled: true, items: [] };
    else if (path === "/me/conversations") {
      if (request.method() === "POST") {
        assert.equal(request.headers()["x-csrf-token"], "test");
        json = { id: "conversation", title: request.postDataJSON().title };
        conversations.push(json);
      } else json = { items: conversations, next_cursor: null };
    } else if (path === "/runs" && request.method() === "POST") {
      const body = request.postDataJSON();
      requests.push(body);
      assert.equal(request.headers()["x-csrf-token"], "test");
      assert.ok(request.headers()["idempotency-key"]);
      json = {
        id: "run", message_sequence: 1, status: "succeeded", error_code: null,
        provider: "mock", model: "mock-v1", charged_tokens: 0,
        input_tokens: null, output_tokens: null, event_sequence: 1,
        events_url: "/api/v1/runs/run/events",
      };
      runs.push(json);
      messages.push(
        { id: "question", sequence: 1, role: "user", content: body.message, run_id: "run" },
        { id: "run", sequence: 2, role: "assistant", content: "模拟回答。" },
      );
    } else if (path === "/conversations/conversation/messages")
      json = { items: messages, next_cursor: null };
    else if (path === "/conversations/conversation/runs") json = { items: runs };
    else if (path === "/runs/run/events") return route.fulfill({
      contentType: "text/event-stream",
      body: 'id: run:1\ndata: {"sequence":1,"type":"completed","payload":{}}\n\n',
    });
    else throw new Error("Unexpected API: " + request.method() + " " + path);
    await route.fulfill({ json });
  });
  await page.goto("http://127.0.0.1:15203/");
  const selected = page.getByLabel("选择助手", { exact: true });
  await selected.locator("option[value='version-second']").waitFor({ state: "attached" });
  assert.equal(await selected.inputValue(), "version-first");
  await selected.selectOption("version-second");
  await page.getByLabel("任务输入", { exact: true }).fill("切换功能时保留的草稿");
  const navigation = page.getByRole("navigation", { name: "主要导航" });
  for (const destination of ["任务记录", "设置"]) {
    const link = destination === "设置"
      ? page.getByRole("button", { name: /设置/ })
      : navigation.getByRole("button", { name: destination, exact: true });
    await link.click();
    await selected.waitFor({ state: "detached" });
    await navigation.getByRole("button", { name: "对话", exact: true }).click();
    await selected.locator("option[value='version-second']").waitFor({ state: "attached" });
    assert.equal(await selected.inputValue(), "version-second");
    assert.equal(await page.getByLabel("任务输入", { exact: true }).inputValue(),
      "切换功能时保留的草稿");
  }
  await page.getByRole("button", { name: "发送任务", exact: true }).click();
  await page.getByText("模拟回答。", { exact: true }).waitFor();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].agent_version_id, "version-second");
  assert.equal(await selected.inputValue(), "version-second");
  await navigation.getByRole("button", { name: "任务记录", exact: true }).click();
  await selected.waitFor({ state: "detached" });
  agents = [agents[0]];
  await navigation.getByRole("button", { name: "对话", exact: true }).click();
  await selected.locator("option[value='version-first']").waitFor({ state: "attached" });
  assert.equal(await selected.inputValue(), "version-first");
  assert.deepEqual(errors, []);
  console.log("PASS: Agent selection survives feature navigation and send remount; submitted version stays selected; unavailable selection falls back.");
} finally {
  await browser?.close();
  await server.close();
}


// 隔离 API 的 Agent 编辑验收；UI_BASE_URL 可指向已更新容器，不读写真实助手。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "../frontend/node_modules/vite/dist/node/index.js";
import { chromium } from "../frontend/node_modules/playwright/index.mjs";

const frontend = fileURLToPath(new URL("../frontend", import.meta.url));
const directory = resolve(frontend, ".cache/agent-workspace-ui");
await mkdir(directory, { recursive: true });
const server = process.env.UI_BASE_URL
  ? null
  : await createServer({
      root: frontend,
      configFile: false,
      server: { host: "127.0.0.1", port: 15179, strictPort: true },
    });
const origin = process.env.UI_BASE_URL || "http://127.0.0.1:15179";
let browser;
try {
  await server?.listen();
  browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await browser.newPage({
    viewport: { width: 1600, height: 1100 },
  });
  const errors = [],
    published = [],
    deleted = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const config = {
    provider: "openai_compatible",
    model: "server-model",
    system_prompt:
      "你是可靠的中文助手。根据任务选择授权工具，清晰回答并说明不确定性。",
    closed_loop: false,
    tools: [
      "text_stats",
      "format_report",
      "traffic_query",
      "weather_query",
      "web_search",
    ],
    token_budget: 30000,
    max_steps: 6,
    max_tool_calls: 10,
    max_output_tokens: 2048,
    timeout_seconds: 120,
  };
  let agents = [
    {
      id: "common",
      name: "通用助手",
      version_id: "common-v7",
      number: 7,
      config: { ...config },
    },
    {
      id: "research",
      name: "论文研究助手",
      version_id: "research-v3",
      number: 3,
      config: {
        ...config,
        base_url: "https://model.example.test/v1",
        model: "research-model",
        has_api_key: true,
        tools: ["text_stats", "web_search"],
      },
    },
    {
      id: "organizer",
      name: "资料整理助手",
      version_id: "organizer-v2",
      number: 2,
      config: { ...config, tools: ["text_stats", "format_report"] },
    },
  ];
  const titles = {
    text_stats: "文本统计",
    format_report: "报告排版",
    traffic_query: "路况查询",
    weather_query: "天气查询",
    web_search: "网页搜索",
  };
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
    if (path.endsWith("/me/tools"))
      json = {
        items: Object.entries(titles).map(([name, title]) => ({
          name,
          title,
          enabled: true,
          ready: true,
          kind: "builtin",
          builtin: true,
        })),
        next_cursor: null,
      };
    if (path.endsWith("/me/agents") && request.method() === "GET")
      json = { items: agents, next_cursor: null };
    if (
      request.method() === "POST" &&
      (path.endsWith("/versions") || path.endsWith("/me/agents"))
    ) {
      const body = request.postDataJSON(),
        id = path.endsWith("/versions") ? path.split("/").at(-2) : "new-agent";
      published.push({ id, body });
      const previous = agents.find((agent) => agent.id === id),
        number = (previous?.number || 0) + 1;
      const updated = {
        id,
        name: body.name,
        number,
        version_id: `${id}-v${number}`,
        config: { ...config, ...previous?.config, ...body },
      };
      delete updated.config.api_key;
      if (body.provider) updated.config.has_api_key = true;
      agents = previous
        ? agents.map((agent) => (agent.id === id ? updated : agent))
        : [...agents, updated];
      json = { id, version_id: updated.version_id, number };
      status = 201;
    }
    if (request.method() === "DELETE") {
      const id = path.split("/").at(-1);
      deleted.push(id);
      agents = agents.filter((agent) => agent.id !== id);
      return route.fulfill({ status: 204 });
    }
    return route.fulfill({ status, json });
  });
  await page.goto(origin);
  await page.getByRole("button", { name: "Agent", exact: true }).click();
  await page.getByText("已发布版本 v7", { exact: true }).waitFor();
  assert.equal(
    await page.getByLabel("助手名称", { exact: true }).inputValue(),
    "通用助手",
  );
  assert.equal(await page.getByRole("dialog").count(), 0);
  await page
    .locator(".av-tool-chips")
    .getByText("文本统计", { exact: true })
    .waitFor();
  await page.screenshot({
    path: resolve(directory, "agents-light.png"),
    fullPage: true,
    animations: "disabled",
  });
  await page.getByRole("button", { name: "切换到深色主题" }).click();
  await page.screenshot({
    path: resolve(directory, "agents-dark.png"),
    fullPage: true,
    animations: "disabled",
  });
  await page.getByRole("button", { name: "切换到浅色主题" }).click();
  await page.getByLabel("搜索助手", { exact: true }).fill("论文");
  assert.equal(await page.locator(".av-select-agent").count(), 1);
  await page.getByLabel("搜索助手", { exact: true }).fill("无此助手");
  await page.getByText("没有匹配的助手。", { exact: true }).waitFor();
  await page.getByLabel("搜索助手", { exact: true }).fill("");
  await page
    .locator(".av-select-agent")
    .filter({ hasText: "论文研究助手" })
    .click();
  const key = page.getByLabel("模型 API Key", { exact: true });
  assert.equal(await key.inputValue(), "");
  assert.equal(await key.getAttribute("required"), null);
  await page
    .getByLabel("模型 URL", { exact: true })
    .fill("https://changed.example.test/v1");
  await page.getByRole("button", { name: "工具权限", exact: true }).click();
  await page.getByRole("button", { name: "发布新版本", exact: true }).click();
  assert.equal(published.length, 0);
  assert.equal(
    await page
      .getByRole("button", { name: "基础配置", exact: true })
      .getAttribute("aria-pressed"),
    "true",
  );
  assert.equal(
    await key.evaluate((element) => element.validity.valueMissing),
    true,
  );
  await page
    .getByLabel("模型 URL", { exact: true })
    .fill("https://model.example.test/v1");
  assert.equal(await key.getAttribute("required"), null);
  await page
    .getByLabel("系统提示词", { exact: true })
    .fill("你是论文研究助手。优先核对原文。");
  await page.getByRole("switch", { name: "规划与检查", exact: true }).check();
  await page.getByRole("button", { name: "工具权限", exact: true }).click();
  await page.getByLabel("天气查询", { exact: true }).check();
  await page.getByRole("button", { name: "运行限制", exact: true }).click();
  await page
    .getByLabel("单次任务预算（保守额度）", { exact: true })
    .fill("100000");
  await page.getByLabel("模型决策上限", { exact: true }).fill("8");
  await page.getByLabel("每次输出 Token 上限", { exact: true }).fill("4096");
  await page.getByLabel("任务超时（秒）", { exact: true }).fill("180");
  await page.getByRole("button", { name: "发布新版本", exact: true }).click();
  await page.getByText("已发布版本 v4", { exact: true }).waitFor();
  assert.equal(published.length, 1);
  assert.equal(published[0].id, "research");
  assert.equal(published[0].body.api_key, "");
  assert.equal(published[0].body.closed_loop, true);
  assert.deepEqual(published[0].body.tools, [
    "text_stats",
    "web_search",
    "weather_query",
  ]);
  assert.equal(published[0].body.token_budget, 100000);
  assert.equal(published[0].body.max_steps, 8);
  assert.equal(published[0].body.max_output_tokens, 4096);
  assert.equal(published[0].body.timeout_seconds, 180);
  assert.equal(await key.inputValue(), "");
  await page.getByRole("button", { name: "＋ 新助手", exact: true }).click();
  await page.getByLabel("助手名称", { exact: true }).fill("资料校对助手");
  await page.getByLabel("模型提供商", { exact: true }).selectOption("mock");
  assert.equal(
    await page.getByLabel("模型名称", { exact: true }).inputValue(),
    "mock-v1",
  );
  await page.getByRole("button", { name: "创建助手", exact: true }).click();
  await page.getByText("已发布版本 v1", { exact: true }).waitFor();
  assert.equal(published[1].body.provider, "mock");
  assert.equal(published[1].body.closed_loop, false);
  assert.equal(published[1].body.token_budget, 30000);
  assert.equal(
    await page
      .getByRole("complementary", { name: "我的助手" })
      .getByRole("button", { name: "＋ 新助手", exact: true })
      .count(),
    1,
  );
  assert.equal(await page.locator(".av-agent-options").count(), 0);
  await page
    .locator(".av-select-agent")
    .filter({ hasText: "通用助手" })
    .click();
  await page.getByRole("button", { name: "删除助手", exact: true }).click();
  await page.getByRole("dialog").waitFor();
  await page.getByRole("button", { name: "取消", exact: true }).click();
  assert.equal(deleted.length, 0);
  assert.equal(
    await page.getByLabel("助手名称", { exact: true }).inputValue(),
    "通用助手",
  );
  await page.getByRole("button", { name: "删除助手", exact: true }).click();
  await page.getByRole("button", { name: "确认删除", exact: true }).click();
  await page
    .getByText("助手已删除，历史会话和任务已保留。", { exact: true })
    .waitFor();
  assert.equal(
    await page.getByLabel("助手名称", { exact: true }).inputValue(),
    "通用助手",
  );
  await page
    .locator(".av-select-agent")
    .filter({ hasText: "资料校对助手" })
    .click();
  await page.getByRole("button", { name: "删除助手", exact: true }).click();
  await page.getByRole("button", { name: "确认删除", exact: true }).click();
  await page.getByText("未发布", { exact: true }).waitFor();
  assert.deepEqual(deleted, ["common", "new-agent"]);
  assert.equal(
    await page.getByLabel("助手名称", { exact: true }).inputValue(),
    "通用助手",
  );
  await page.setViewportSize({ width: 400, height: 850 });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
    true,
  );
  assert.equal(
    await page
      .locator(".agents-workspace")
      .evaluate((element) => element.scrollWidth <= element.clientWidth),
    true,
  );
  await page.locator(".agents-workspace").evaluate((element) => {
    element.scrollTop = 0;
  });
  await page.screenshot({
    path: resolve(directory, "agents-mobile.png"),
    fullPage: true,
    animations: "disabled",
  });
  assert.deepEqual(errors, []);
  console.log(
    JSON.stringify({
      passed: true,
      target: origin,
      checks: [
        "default-selection",
        "inline-editor",
        "search",
        "key-reuse",
        "changed-url-key-required",
        "hidden-tab-validation",
        "tools",
        "limits",
        "publish-version",
        "create",
        "delete-confirm",
        "original-direct-actions",
        "delete-current-reset",
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

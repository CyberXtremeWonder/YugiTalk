// Skill/MCP/Tool 的隔离浏览器验收；可用 UI_BASE_URL 检查容器资源，不修改真实数据。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "../frontend/node_modules/vite/dist/node/index.js";
import { chromium } from "../frontend/node_modules/playwright/index.mjs";

const frontend = fileURLToPath(new URL("../frontend", import.meta.url));
const directory = resolve(frontend, ".cache/configuration-workspaces-ui");
await mkdir(directory, { recursive: true });
const server = process.env.UI_BASE_URL
  ? null
  : await createServer({
      root: frontend,
      configFile: false,
      server: { host: "127.0.0.1", port: 15180, strictPort: true },
    });
const origin = process.env.UI_BASE_URL || "http://127.0.0.1:15180";
let browser;
try {
  await server?.listen();
  browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await browser.newPage({
    viewport: { width: 1600, height: 1100 },
  });
  const errors = [],
    writes = [],
    validations = [],
    detailReads = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let admin = true,
    failDiscovery = false,
    discovered = 0;
  const toolConfig = {
    name: "document_lookup",
    title: "文档查询",
    description: "按关键词查询外部文档目录。",
    endpoint: "https://api.example.test/search",
    method: "GET",
    parameters: [
      { name: "query", type: "string", description: "关键词", required: true },
    ],
    required_roles: ["owner", "admin", "member"],
    secret_name: "",
    timeout_seconds: 10,
    max_response_bytes: 8000,
  };
  let tools = [
    ...Object.entries({
      text_stats: "文本统计",
      format_report: "报告排版",
      traffic_query: "路况查询",
      weather_query: "天气查询",
      web_search: "网页搜索",
    }).map(([name, title]) => ({
      name,
      title,
      kind: "builtin",
      builtin: true,
      number: 1,
      enabled: true,
      ready: !["traffic_query", "weather_query"].includes(name),
      can_manage: true,
      description: `${title}工具`,
      schema: {
        parameters: {
          type: "object",
          properties: { text: { type: "string" } },
        },
      },
    })),
    {
      name: "document_lookup",
      title: "文档查询",
      kind: "http",
      builtin: false,
      number: 2,
      enabled: true,
      ready: true,
      can_manage: true,
      description: toolConfig.description,
      config: toolConfig,
      schema: { parameters: {} },
    },
    {
      name: "shared_lookup",
      title: "公共共享查询",
      kind: "http",
      builtin: false,
      number: 1,
      enabled: true,
      ready: true,
      can_manage: false,
      description: "其他创建者的查询工具",
      schema: { parameters: {} },
    },
    ...["search", "fetch", "list"].map((name, i) => ({
      name: `mcp_${name}`,
      title: ["搜索文档", "读取原文", "列出集合"][i],
      kind: "mcp",
      builtin: false,
      number: 1,
      enabled: i < 2,
      ready: true,
      can_manage: true,
      description: "由 MCP 服务器提供",
      schema: { parameters: { type: "object" } },
    })),
  ];
  let skills = [
    {
      id: "paper",
      name: "paper-review",
      description: "论文阅读与评审",
      number: 3,
      version_id: "paper-v3",
      enabled: true,
      can_manage: true,
      allowed_tools: ["text_stats", "web_search"],
      instructions:
        "# 论文阅读流程\n\n1. 确认研究问题。\n2. 核对原文证据。\n3. 输出结论与局限。",
    },
    {
      id: "weekly",
      name: "weekly-report",
      description: "每周工作汇总",
      number: 1,
      enabled: true,
      can_manage: true,
      allowed_tools: ["format_report"],
      instructions: "# 周报\n整理本周进展。",
    },
    {
      id: "meeting",
      name: "meeting-notes",
      description: "会议纪要整理",
      number: 1,
      enabled: true,
      can_manage: false,
      allowed_tools: [],
      instructions: "# 会议纪要\n整理议题与待办。",
    },
  ];
  let servers = [
    {
      id: "docs",
      name: "文档检索服务",
      url: "https://mcp.example.test/mcp",
      transport: "streamable_http",
      enabled: true,
      has_token: true,
      tools: tools.filter((t) => t.kind === "mcp"),
    },
    {
      id: "web",
      name: "网页抓取服务",
      url: "https://web.example.test/sse",
      transport: "sse",
      enabled: true,
      has_token: false,
      tools: [],
    },
    {
      id: "team",
      name: "团队资料服务",
      url: "https://team.example.test/mcp",
      transport: "streamable_http",
      enabled: false,
      has_token: false,
      tools: [],
    },
  ];
  let submissions = [];
  await page.route("**/api/v1/**", (route) => {
    const request = route.request(),
      url = new URL(request.url()),
      path = url.pathname.replace("/api/v1", ""),
      method = request.method();
    const body =
      method === "GET" || method === "DELETE"
        ? undefined
        : request.postDataJSON();
    let json = { items: [], next_cursor: null },
      status = 200;
    if (method !== "GET") {
      assert.equal(request.headers()["x-csrf-token"], "test");
      writes.push({ path, method, body });
    }
    if (path === "/auth/me")
      json = {
        user: { id: "user", name: "测试用户", platform_admin: admin },
        csrf_token: "test",
      };
    if (path === "/me/skills") {
      if (method === "GET")
        json = {
          items: skills.map(({ instructions, ...row }) => row),
          next_cursor: null,
        };
      else {
        json = {
          ...body,
          id: "new-skill",
          version_id: "new-v1",
          number: 1,
          can_manage: true,
        };
        skills.push(json);
        status = 201;
      }
    }
    if (path.startsWith("/skills/")) {
      const id = path.split("/")[2],
        item = skills.find((row) => row.id === id);
      if (method === "GET") {
        detailReads.push(id);
        json = item;
      }
      if (method === "POST") {
        json = { ...item, ...body, number: item.number + 1 };
        skills = skills.map((row) => (row.id === id ? json : row));
        status = 201;
      }
      if (method === "PATCH") {
        json = { ...item, ...body };
        skills = skills.map((row) => (row.id === id ? json : row));
      }
      if (method === "DELETE") {
        skills = skills.filter((row) => row.id !== id);
        return route.fulfill({ status: 204 });
      }
    }
    if (path === "/me/mcp-servers") {
      if (method === "GET") json = { items: servers, next_cursor: null };
      else {
        json = {
          ...body,
          id: "new-server",
          has_token: !!body.token,
          tools: [],
        };
        delete json.token;
        servers.push(json);
        status = 201;
      }
    }
    if (path.startsWith("/me/mcp-servers/")) {
      const id = path.split("/")[3],
        item = servers.find((row) => row.id === id);
      if (path.endsWith("/discover")) {
        discovered++;
        if (failDiscovery)
          return route.fulfill({
            status: 503,
            json: { detail: "unavailable" },
          });
        item.tools = item.tools.map((tool) => ({ ...tool, enabled: false }));
        json = item;
      } else if (method === "DELETE") {
        servers = servers.filter((row) => row.id !== id);
        return route.fulfill({ status: 204 });
      } else {
        json = {
          ...item,
          ...body,
          has_token: Object.hasOwn(body, "token")
            ? !!body.token
            : item.has_token,
        };
        delete json.token;
        servers = servers.map((row) => (row.id === id ? json : row));
      }
    }
    if (path === "/me/tools") {
      if (method === "GET") json = { items: tools, next_cursor: null };
      else {
        if (!admin) return route.fulfill({ status: 403, json: {} });
        json = {
          name: body.name,
          title: body.title,
          description: body.description,
          kind: "http",
          builtin: false,
          number: 1,
          enabled: true,
          ready: true,
          can_manage: true,
          config: body,
          schema: { parameters: {} },
        };
        tools.push(json);
        status = 201;
      }
    }
    if (path.startsWith("/me/tools/")) {
      const name = path.split("/")[3],
        item = tools.find((row) => row.name === name);
      if (path.endsWith("/validate")) {
        validations.push(body);
        json = { valid: true };
      } else if (method === "POST") {
        if (!admin) return route.fulfill({ status: 403, json: {} });
        json = {
          ...item,
          title: body.title,
          number: item.number + 1,
          config: body,
        };
        tools = tools.map((row) => (row.name === name ? json : row));
        status = 201;
      } else if (method === "DELETE") {
        tools = tools.filter((row) => row.name !== name);
        return route.fulfill({ status: 204 });
      } else {
        json = { ...item, ...body };
        tools = tools.map((row) => (row.name === name ? json : row));
        servers = servers.map((row) => ({
          ...row,
          tools: row.tools.map((tool) =>
            tool.name === name ? { ...tool, ...body } : tool,
          ),
        }));
      }
    }
    if (path === "/me/tool-submissions") {
      if (method === "GET") json = { items: submissions, next_cursor: null };
      else {
        json = {
          id: "submission",
          status: "pending",
          reason: "",
          config: body,
        };
        submissions.push(json);
        status = 201;
      }
    }
    return route.fulfill({ status, json });
  });
  const nav = async (name) =>
    page
      .locator("#navigation")
      .getByRole("button", { name, exact: true })
      .click();
  const capture = async (name) => {
    await page.screenshot({
      path: resolve(directory, `${name}-light.png`),
      fullPage: true,
      animations: "disabled",
    });
    await page.getByRole("button", { name: "切换到深色主题" }).click();
    await page.screenshot({
      path: resolve(directory, `${name}-dark.png`),
      fullPage: true,
      animations: "disabled",
    });
    await page.getByRole("button", { name: "切换到浅色主题" }).click();
  };
  await page.goto(origin);
  // Skill: 详情读取、只读成员、版本、工具交集与即时启停。
  await nav("Skill");
  await page.getByLabel("Skill 名称", { exact: true }).waitFor();
  await page.waitForFunction(() =>
    document
      .querySelector('textarea[aria-label="技能流程（Markdown）"]')
      ?.value.includes("论文"),
  );
  assert.ok(detailReads.includes("paper"));
  await capture("skill");
  await page.getByLabel("搜索技能").fill("weekly");
  assert.equal(await page.locator(".cf-select").count(), 1);
  await page.getByLabel("搜索技能").fill("");
  await page.locator(".cf-select").filter({ hasText: "meeting-notes" }).click();
  await page.waitForFunction(
    () => document.querySelector("input[pattern]")?.value === "meeting-notes",
  );
  assert.equal(
    await page.getByLabel("Skill 名称", { exact: true }).isDisabled(),
    true,
  );
  assert.equal(
    await page
      .getByRole("button", { name: "发布新版本", exact: true })
      .isDisabled(),
    true,
  );
  await page.locator(".cf-select").filter({ hasText: "paper-review" }).click();
  await page.waitForFunction(
    () => document.querySelector("input[pattern]")?.value === "paper-review",
  );
  await page
    .getByLabel("技能流程（Markdown）", { exact: true })
    .fill("# 工作流程\n1. 核对原文，再输出结论。");
  await page
    .getByRole("button", { name: "管理工具范围 →", exact: true })
    .click();
  await page.getByLabel("报告排版", { exact: true }).check();
  await page.getByRole("button", { name: "发布新版本", exact: true }).click();
  await page.getByText(/Skill 已发布：paper-review · v4/).waitFor();
  assert.deepEqual(
    writes.find((w) => w.path === "/skills/paper/versions").body.allowed_tools,
    ["text_stats", "web_search", "format_report"],
  );
  await page
    .getByRole("button", { name: "立即停用 Skill", exact: true })
    .click();
  await page.getByText("Skill 已停用。", { exact: true }).waitFor();
  await page.getByRole("button", { name: "＋ 新 Skill", exact: true }).click();
  await page.getByLabel("Skill 名称", { exact: true }).fill("project-review");
  await page.getByLabel("技能描述", { exact: true }).fill("项目审阅流程");
  await page.getByRole("button", { name: "创建 Skill", exact: true }).click();
  await page.getByText(/Skill 已发布：project-review/).waitFor();
  await page.getByRole("button", { name: "删除 Skill", exact: true }).click();
  await page.getByText("Skill 已删除。", { exact: true }).waitFor();
  // MCP: 留空保留、清除与替换、修改后禁止发现、失败不会伪报连接成功。
  await nav("MCP");
  await page.waitForFunction(
    () =>
      document.querySelector('input[maxlength="80"]')?.value === "文档检索服务",
  );
  await capture("mcp");
  const token = page.getByLabel("Bearer Token（可选）", { exact: true });
  assert.equal(await token.inputValue(), "");
  await page
    .getByLabel("服务器名称", { exact: true })
    .fill("文档检索服务（更新）");
  assert.equal(
    await page
      .getByRole("button", { name: "测试连接并发现工具", exact: true })
      .isDisabled(),
    true,
  );
  assert.equal(discovered, 0);
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await page.getByText(/已保存。配置变更后/).waitFor();
  assert.equal(
    Object.hasOwn(
      writes.find((w) => w.path === "/me/mcp-servers/docs").body,
      "token",
    ),
    false,
  );
  await page.getByLabel("清除已保存 Token", { exact: true }).check();
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await page.waitForFunction(
    () => !document.body.textContent.includes("清除已保存 Token"),
  );
  assert.equal(
    writes.filter((w) => w.path === "/me/mcp-servers/docs").at(-1).body.token,
    "",
  );
  await token.fill("synthetic-new-token");
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await page.getByLabel("清除已保存 Token", { exact: true }).waitFor();
  assert.equal(await token.inputValue(), "");
  assert.equal(
    await page.getByText("synthetic-new-token", { exact: true }).count(),
    0,
  );
  failDiscovery = true;
  await page
    .getByRole("button", { name: "测试连接并发现工具", exact: true })
    .click();
  await page.locator('.cf-notice[data-error="true"]').waitFor();
  failDiscovery = false;
  await page
    .getByRole("button", { name: "测试连接并发现工具", exact: true })
    .click();
  await page.getByText(/连接成功，已更新工具列表/).waitFor();
  assert.equal(
    await page
      .getByRole("switch", { name: "启用工具 搜索文档", exact: true })
      .isChecked(),
    false,
  );
  await page
    .getByRole("switch", { name: "启用工具 搜索文档", exact: true })
    .click();
  await page.getByText("工具授权已更新。", { exact: true }).waitFor();
  assert.equal(
    await page
      .getByRole("switch", { name: "启用工具 搜索文档", exact: true })
      .isChecked(),
    true,
  );
  await page.getByRole("switch", { name: "启用服务器", exact: true }).click();
  await page.getByText("服务器状态已更新。", { exact: true }).waitFor();
  assert.equal(
    await page
      .getByRole("switch", { name: "启用服务器", exact: true })
      .isChecked(),
    false,
  );
  assert.equal(
    await page
      .getByRole("button", { name: "测试连接并发现工具", exact: true })
      .isDisabled(),
    true,
  );
  assert.equal(
    await page.locator(".cf-catalog").getByRole("button", { name: /^删除服务器：/ }).count(),
    0,
  );
  await page.locator(".cf-catalog .cf-select").filter({ hasText: "团队资料服务" }).click();
  const deleteServer = page.locator(".cf-summary-rail > .cf-card").last()
    .getByRole("button", { name: "删除服务器：团队资料服务", exact: true });
  await deleteServer.click();
  await page.getByRole("button", { name: "取消", exact: true }).click();
  assert.equal(await page.getByLabel("服务器名称", { exact: true }).inputValue(), "团队资料服务");
  await deleteServer.click();
  await page.getByRole("button", { name: "确认删除", exact: true }).click();
  await page
    .getByText("服务器已删除，凭证和调用权限已撤销。", { exact: true })
    .waitFor();
  assert.equal(
    await page.getByLabel("服务器名称", { exact: true }).inputValue(),
    "",
  );
  assert.equal(await page.getByRole("button", { name: /^删除服务器：/ }).count(), 0);
  await page
    .getByRole("button", { name: "＋ 添加 MCP 服务器", exact: true })
    .click();
  assert.equal(await page.getByRole("button", { name: /^删除服务器：/ }).count(), 0);
  await page.getByLabel("服务器名称", { exact: true }).fill("新 SSE 服务");
  await page
    .getByLabel("服务器 URL", { exact: true })
    .fill("https://new.example.test/sse");
  await page.getByLabel("传输类型", { exact: true }).selectOption("sse");
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await page.getByText(/已保存。配置变更后/).waitFor();
  assert.equal(
    writes.find((w) => w.path === "/me/mcp-servers" && w.method === "POST").body
      .transport,
    "sse",
  );
  // Tool: 三种来源、只读边界、管理员发布、参数校验与成员审核。
  await nav("Tool");
  await page.locator(".cf-tool-table tbody tr").first().waitFor();
  assert.equal(
    await page.getByText(/内置工具保留服务端实现/).isVisible(),
    true,
  );
  for (const kind of ["builtin", "http", "mcp"]) {
    const title =
      kind === "builtin" ? "内置" : kind === "http" ? "HTTP" : "MCP";
    await page.locator(".cf-filters button").filter({ hasText: title }).click();
    assert.equal(
      await page.locator(".cf-tool-table tbody tr").count(),
      tools.filter((t) => t.kind === kind).length,
    );
  }
  await page.locator(".cf-filters button").filter({ hasText: "全部" }).click();
  await page
    .locator(".cf-table-select")
    .filter({ hasText: "公共共享查询" })
    .click();
  assert.equal(
    await page.getByLabel("显示名称", { exact: true }).isDisabled(),
    true,
  );
  assert.equal(
    await page.getByRole("button", { name: "发布新版本", exact: true }).count(),
    0,
  );
  await page
    .locator(".cf-table-select")
    .filter({ hasText: "文档查询" })
    .click();
  await capture("tool");
  await page.getByLabel("搜索工具名称").fill("document_lookup");
  assert.equal(await page.locator(".cf-tool-table tbody tr").count(), 1);
  await page.getByLabel("搜索工具名称").fill("");
  await page.getByRole("button", { name: "参数", exact: true }).click();
  await page
    .getByLabel("参数定义（JSON 数组）", { exact: true })
    .fill("[null]");
  assert.equal(
    await page.getByText("暂无有效参数定义。", { exact: true }).isVisible(),
    true,
  );
  await page.getByLabel("参数定义（JSON 数组）", { exact: true }).fill(
    JSON.stringify([
      ...toolConfig.parameters,
      {
        name: "limit",
        type: "integer",
        description: "条数",
        required: false,
      },
    ]),
  );
  await page.getByRole("button", { name: "配置", exact: true }).click();
  await page.getByLabel("显示名称", { exact: true }).fill("文档查询（新版）");
  await page.getByLabel("请求方式", { exact: true }).selectOption("POST");
  await page.locator(".cf-advanced summary").click();
  await page.getByLabel("响应容量（字节）", { exact: true }).fill("10");
  await page.locator(".cf-advanced summary").click();
  const beforeVersions = writes.filter(
    (w) => w.path.endsWith("/versions") && w.path.includes("/me/tools/"),
  ).length;
  await page.getByRole("button", { name: "发布新版本", exact: true }).click();
  assert.equal(await page.locator(".cf-advanced").getAttribute("open"), "");
  assert.equal(
    writes.filter(
      (w) => w.path.endsWith("/versions") && w.path.includes("/me/tools/"),
    ).length,
    beforeVersions,
  );
  await page.getByLabel("响应容量（字节）", { exact: true }).fill("16000");
  await page.getByLabel("普通成员", { exact: true }).uncheck();
  await page.getByRole("button", { name: "发布新版本", exact: true }).click();
  await page.getByText(/Tool 已发布：文档查询（新版） · v3/).waitFor();
  const published = writes.find(
    (w) => w.path === "/me/tools/document_lookup/versions",
  ).body;
  assert.equal(published.method, "POST");
  assert.equal(published.parameters.length, 2);
  assert.deepEqual(published.required_roles, ["owner", "admin"]);
  assert.equal(
    await page.getByLabel("示例参数（仅校验）", { exact: true }).count(),
    0,
  );
  await page.getByRole("button", { name: "校验", exact: true }).click();
  assert.equal(
    await page.getByLabel("显示名称", { exact: true }).isVisible(),
    false,
  );
  await page
    .getByLabel("示例参数（仅校验）", { exact: true })
    .fill('{"query":"Agent","limit":5}');
  await page.getByRole("button", { name: "校验参数", exact: true }).click();
  await page
    .getByText("参数校验通过；未调用外部服务。", { exact: true })
    .waitFor();
  assert.deepEqual(validations, [{ query: "Agent", limit: 5 }]);
  await page
    .getByRole("switch", { name: "启用工具 文档查询（新版）", exact: true })
    .click();
  await page.getByText("工具启用状态已更新。", { exact: true }).waitFor();
  assert.equal(
    await page
      .getByRole("switch", { name: "启用工具 文档查询（新版）", exact: true })
      .isChecked(),
    false,
  );
  assert.equal(await page.locator(".cf-menu").count(), 0);
  await page
    .getByRole("region", { name: "工具目录", exact: true })
    .getByRole("button", { name: "＋ 新工具", exact: true })
    .click();
  await page.getByLabel("工具标识", { exact: true }).fill("quick_lookup");
  await page.getByLabel("显示名称", { exact: true }).fill("临时查询");
  await page.getByLabel("用途说明", { exact: true }).fill("管理员直接新增");
  await page
    .getByLabel("HTTPS 查询地址", { exact: true })
    .fill("https://quick.example.test/search");
  await page.getByRole("button", { name: "创建 Tool", exact: true }).click();
  await page.getByText(/Tool 已发布：临时查询 · v1/).waitFor();
  await page.getByRole("button", { name: "删除 Tool", exact: true }).click();
  await page.getByText("Tool 已删除。", { exact: true }).waitFor();
  assert.equal(
    tools.some((tool) => tool.name === "quick_lookup"),
    false,
  );
  admin = false;
  await page.reload();
  await page.locator(".cf-tool-table tbody tr").first().waitFor();
  await page
    .getByRole("button", { name: "＋ 提交工具审核", exact: true })
    .click();
  await page.getByLabel("工具标识", { exact: true }).fill("member_query");
  await page.getByLabel("显示名称", { exact: true }).fill("成员查询");
  await page.getByLabel("用途说明", { exact: true }).fill("成员提交的查询工具");
  await page
    .getByLabel("HTTPS 查询地址", { exact: true })
    .fill("https://member.example.test/search");
  await page.getByRole("button", { name: "提交审核", exact: true }).click();
  await page
    .getByText("工具已提交审核，批准后才会加入工具目录。", { exact: true })
    .waitFor();
  assert.equal(
    tools.some((t) => t.name === "member_query"),
    false,
  );
  assert.equal(submissions[0].config.enabled, false);
  await page.getByRole("button", { name: "我的提交", exact: true }).click();
  await page.getByText("待审核", { exact: true }).waitFor();
  await page.setViewportSize({ width: 400, height: 850 });
  for (const name of ["Tool", "Skill", "MCP"]) {
    if (name !== "Tool") {
      await page.locator("#menu").click();
      await nav(name);
    }
    assert.equal(
      await page
        .locator(".configuration-workspace")
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
      path: resolve(directory, `${name.toLowerCase()}-mobile.png`),
      fullPage: true,
      animations: "disabled",
    });
  }
  assert.deepEqual(errors, []);
  console.log(
    JSON.stringify({
      passed: true,
      target: origin,
      checks: [
        "skill-detail-read",
        "skill-readonly",
        "skill-version",
        "skill-tools",
        "skill-toggle-create-delete",
        "mcp-token-retain-clear-replace",
        "mcp-unsaved-blocks-discovery",
        "mcp-failure",
        "mcp-tool-enable",
        "mcp-server-disable",
        "mcp-delete-confirm",
        "mcp-sse-create",
        "tool-source-filters",
        "tool-builtin-readonly",
        "tool-member-permissions",
        "tool-parameter-edit",
        "tool-hidden-validation",
        "tool-version",
        "tool-validation-only",
        "tool-toggle",
        "member-review",
        "three-themes-mobile",
      ],
      screenshots: directory,
    }),
  );
} finally {
  await browser?.close();
  await server?.close();
}

// 管理员工作台的隔离浏览器验收；所有管理请求均由本地夹具接管。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "../frontend/node_modules/vite/dist/node/index.js";
import { chromium } from "../frontend/node_modules/playwright/index.mjs";

const frontend = fileURLToPath(new URL("../frontend", import.meta.url));
const directory = resolve(frontend, ".cache/admin-workspace-ui");
await mkdir(directory, { recursive: true });
const server = await createServer({
  root: frontend,
  configFile: false,
  server: { host: "127.0.0.1", port: 15183, strictPort: true },
});
const origin = "http://127.0.0.1:15183";
const csrf = "admin-ui-csrf";
const accounts = [
  { id: "admin", name: "项目管理员", email: "admin@example.test", active: true, platform_admin: true },
  { id: "member", name: "林知夏", email: "zhixia@example.test", active: true, platform_admin: false },
  { id: "disabled", name: "陈一舟", email: "yizhou@example.test", active: false, platform_admin: false },
  { id: "next", name: "周亦宁", email: "yining@example.test", active: true, platform_admin: false },
];
const tool = (name, title) => ({
  id: name,
  account_id: "林知夏的个人空间",
  submitter_id: "member",
  config: {
    name, title, description: "按关键词查询文档目录，返回文档标题与原文链接。",
    endpoint: `https://api.example.test/${name}`, method: "GET",
    parameters: [{ name: "query", type: "string", description: "查询关键词", required: true }],
    required_roles: ["owner", "admin", "member"], secret_name: "",
    timeout_seconds: 10, max_response_bytes: 8000,
  },
});
let submissions = [tool("search_docs", "文档查询"), tool("read_catalog", "目录读取"), tool("project_lookup", "项目信息查询")];
let platformAdmin = true, developmentMode = true, empty = false;
let failStatus = false, failToggle = false, failReview = false, showSubmissions = true;
let redisReady = true, healthRequests = 0;
let releaseUsers;
let heldUsers = null;
const writes = [], errors = [], adminReads = [];
let browser;
try {
  await server.listen();
  browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1450 }, reducedMotion: "reduce" });
  await page.clock.install();
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("https://api.example.test/**", (route) => {
    errors.push("管理员审核不应访问外部工具端点");
    return route.abort();
  });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request(), url = new URL(request.url());
    const path = url.pathname.replace("/api/v1", ""), method = request.method();
    const cursor = url.searchParams.get("cursor");
    let json = { items: [], next_cursor: null };
    if (path === "/auth/me") json = {
      user: { id: "admin", name: "项目管理员", email: "admin@example.test", platform_admin: platformAdmin },
      csrf_token: csrf, development_mode: developmentMode,
    };
    if (path.startsWith("/admin/") && method === "GET") adminReads.push(path);
    if (path === "/admin/development/status") {
      if (failStatus) return route.fulfill({ status: 503, json: { detail: "unavailable" } });
      const checkedAt = new Date(Date.UTC(2026, 10, 3, 2, 0, ++healthRequests)).toISOString();
      const checks = { postgres: true, redis: redisReady, storage: true, milvus: true, worker: true, docling: true, embedding: true, api: true };
      json = {
        environment: "development", model_provider: "openai_compatible", model_id: "deepseek-flash",
        embedding_model: "BAAI/bge-m3", registration_ready: true,
        checks,
        check_details: Object.fromEntries(Object.entries(checks).map(([name, ready], index) => [name, {
          ready, latency_ms: ready ? 3.5 + index * 2 : 1000,
          checked_at: checkedAt,
          description: { postgres: "主数据库", redis: "缓存与任务队列", storage: "文件存储", milvus: "向量数据库", worker: "后台任务 Worker", docling: "文档解析服务", embedding: "本地 BGE 嵌入服务", api: "API 响应" }[name],
        }])),
        checked_at: checkedAt,
      };
    }
    if (path === "/admin/users") {
      if (heldUsers) await heldUsers;
      json = {
        items: empty ? [] : cursor ? accounts.slice(3) : accounts.slice(0, 3),
        next_cursor: empty || cursor ? null : "users:next",
      };
    }
    if (path === "/admin/audit") json = { items: empty ? [] : [
      { id: "audit-1", action: "user.update", created_at: "2026-11-03T01:35:00Z" },
      { id: "audit-2", action: "tool.review", created_at: "2026-11-03T01:20:00Z" },
      { id: "audit-3", action: "auth.development_mode", created_at: "2026-11-03T00:50:00Z" },
    ] };
    if (path === "/admin/tool-submissions") json = {
      items: empty || !showSubmissions ? [] : cursor ? submissions.slice(2) : submissions.slice(0, 2),
      next_cursor: empty || !showSubmissions || cursor || submissions.length < 3 ? null : "reviews:next",
    };
    if (method === "PATCH" && path.startsWith("/admin/users/")) {
      const body = request.postDataJSON();
      writes.push({ path, method, body, csrf: request.headers()["x-csrf-token"] });
      if (failToggle) return route.fulfill({ status: 409, json: { detail: "conflict" } });
      const account = accounts.find((item) => path.endsWith(`/${item.id}`));
      assert.ok(account && !account.platform_admin, "管理员账号不得触发停用请求");
      account.active = body.active;
      json = account;
    }
    if (method === "POST" && path.endsWith("/review")) {
      const body = request.postDataJSON();
      writes.push({ path, method, body, csrf: request.headers()["x-csrf-token"] });
      if (failReview) return route.fulfill({ status: 409, json: { detail: "conflict" } });
      const id = path.split("/").at(-2);
      submissions = submissions.filter((item) => item.id !== id);
      json = { status: body.decision };
    }
    return route.fulfill({ json });
  });
  const users = page.locator(".admin-users");
  const services = page.locator(".admin-services");
  const service = (name) => services.locator(".admin-service").filter({ has: page.getByText(new RegExp(`^${name}$`, "i")) });
  const checkedTime = page.locator(".admin-health-updated time");
  const accountRow = (email) => users.locator("tbody tr").filter({ hasText: email });
  const reviewCard = (name) => page.locator(".admin-review-card").filter({ has: page.getByLabel(`审核意见 ${name}`, { exact: true }) });
  const screenshot = (name) => page.screenshot({ path: resolve(directory, name), fullPage: true, animations: "disabled" });
  const open = async () => {
    await page.goto(`${origin}/#development`);
    await page.locator(".admin-panel").waitFor();
  };

  // 账号加载与服务健康独立；延迟账号响应，确认列表有明确加载状态。
  heldUsers = new Promise((resolveUsers) => { releaseUsers = resolveUsers; });
  await open();
  await page.getByText("正在处理…", { exact: true }).waitFor();
  assert.equal(await page.locator(".admin-panel").getAttribute("aria-busy"), "true");
  releaseUsers(); heldUsers = null;
  await accountRow("admin@example.test").waitFor();
  await page.locator(".admin-services").waitFor();
  assert.equal(await page.locator(".admin-summary .admin-stat").count(), 3);
  assert.ok((await page.locator(".admin-summary").innerText()).includes("已加载账号"));
  assert.equal(await users.locator("tbody tr").count(), 3);
  assert.equal(await accountRow("admin@example.test").getByRole("button", { name: "停用", exact: true }).isDisabled(), true);
  const panelWidth = await page.locator(".admin-container").evaluate((element) => element.getBoundingClientRect().width);
  assert.ok(panelWidth > 900, `管理员面板仍过窄：${panelWidth}px`);
  await reviewCard("search_docs").waitFor();
  await screenshot("admin-reviews.png");

  await page.getByLabel("搜索账号", { exact: true }).fill("ZHIXIA@");
  assert.equal(await users.locator("tbody tr").count(), 1);
  assert.ok((await users.locator("tbody tr").innerText()).includes("林知夏"));
  await page.getByLabel("搜索账号", { exact: true }).fill("不存在的账号");
  await page.getByText("没有匹配的账号，请调整搜索条件。", { exact: true }).waitFor();
  await page.getByLabel("搜索账号", { exact: true }).fill("");
  await users.getByRole("button", { name: "加载更多账号", exact: true }).click();
  await accountRow("yining@example.test").waitFor();
  assert.equal(await users.locator("tbody tr").count(), 4);
  assert.equal(await users.getByRole("button", { name: "加载更多账号", exact: true }).count(), 0);

  await accountRow("zhixia@example.test").getByRole("button", { name: "停用", exact: true }).click();
  await accountRow("zhixia@example.test").getByRole("button", { name: "启用", exact: true }).waitFor();
  assert.deepEqual(writes.at(-1), { path: "/admin/users/member", method: "PATCH", body: { active: false }, csrf });
  failToggle = true;
  await accountRow("zhixia@example.test").getByRole("button", { name: "启用", exact: true }).click();
  await page.locator(".admin-panel > .admin-container > [role=alert]").waitFor();
  assert.equal(await accountRow("zhixia@example.test").getByRole("button", { name: "启用", exact: true }).count(), 1);
  failToggle = false;
  await accountRow("zhixia@example.test").getByRole("button", { name: "启用", exact: true }).click();
  await accountRow("zhixia@example.test").getByRole("button", { name: "停用", exact: true }).waitFor();

  await reviewCard("search_docs").waitFor();
  const writesBeforeRejection = writes.length;
  await reviewCard("search_docs").getByRole("button", { name: "驳回", exact: true }).click();
  await page.getByText("驳回时请填写审核意见。", { exact: true }).waitFor();
  assert.equal(writes.length, writesBeforeRejection);
  await page.getByRole("button", { name: "加载更多待审工具", exact: true }).click();
  await reviewCard("project_lookup").waitFor();
  failReview = true;
  await reviewCard("search_docs").getByRole("button", { name: "批准", exact: true }).click();
  await page.locator("[aria-labelledby=tool-review-title] [role=alert]").waitFor();
  assert.equal(await reviewCard("search_docs").count(), 1);
  failReview = false;
  await reviewCard("search_docs").getByRole("button", { name: "批准", exact: true }).click();
  await reviewCard("search_docs").waitFor({ state: "detached" });
  assert.deepEqual(writes.at(-1), { path: "/admin/tool-submissions/search_docs/review", method: "POST", body: { decision: "approved", reason: "" }, csrf });
  await page.getByLabel("审核意见 read_catalog", { exact: true }).fill("请补充参数说明。");
  await reviewCard("read_catalog").getByRole("button", { name: "驳回", exact: true }).click();
  await reviewCard("read_catalog").waitFor({ state: "detached" });
  assert.deepEqual(writes.at(-1), { path: "/admin/tool-submissions/read_catalog/review", method: "POST", body: { decision: "rejected", reason: "请补充参数说明。" }, csrf });

  // 主截图采用常见的无待审工具状态，避免测试动作的提示遮挡界面。
  showSubmissions = false;
  await page.reload();
  await accountRow("admin@example.test").waitFor();
  await page.getByText("暂无待审核工具。", { exact: true }).waitFor();
  await service("redis").getByText("正常", { exact: true }).waitFor();
  await users.getByRole("button", { name: "加载更多账号", exact: true }).click();
  await accountRow("yining@example.test").waitFor();
  await page.evaluate(() => {
    window.scrollTo(0, 0);
    for (const element of document.querySelectorAll(".admin-panel, .main-panel")) element.scrollTop = 0;
  });
  await screenshot("admin-light.png");
  await page.getByRole("button", { name: "切换到深色主题", exact: true }).click();
  await screenshot("admin-dark.png");
  await page.getByRole("button", { name: "切换到浅色主题", exact: true }).click();
  await page.setViewportSize({ width: 400, height: 1000 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  assert.equal(await page.locator(".admin-panel").evaluate((element) => element.scrollWidth <= element.clientWidth), true);
  await screenshot("admin-mobile.png");
  await page.setViewportSize({ width: 1600, height: 1450 });

  // 手动刷新覆盖健康变化、失效快照与恢复，无需等待 15 秒轮询周期。
  const initialCheckedAt = await checkedTime.getAttribute("datetime");
  assert.ok(initialCheckedAt, "健康信息必须显示检查时间");
  redisReady = false;
  await page.getByRole("button", { name: "刷新状态", exact: true }).click();
  await service("redis").getByText("异常", { exact: true }).waitFor();
  assert.equal(await service("redis").locator(".admin-badge.good").count(), 0);
  const warningCheckedAt = await checkedTime.getAttribute("datetime");
  assert.notEqual(warningCheckedAt, initialCheckedAt);
  await page.evaluate(() => {
    window.scrollTo(0, 0);
    for (const element of document.querySelectorAll(".admin-panel, .main-panel")) element.scrollTop = 0;
  });
  await screenshot("admin-health-warning.png");
  failStatus = true;
  await page.getByRole("button", { name: "刷新状态", exact: true }).click();
  await page.getByText("状态已过期", { exact: true }).first().waitFor();
  await service("redis").getByText("未知", { exact: true }).waitFor();
  assert.equal(await services.locator(".admin-badge.good").count(), 0, "健康读取失败后不得继续显示绿色旧快照");
  assert.equal(await users.locator("tbody tr").count(), 4);
  failStatus = false; redisReady = true;
  await page.getByRole("button", { name: "刷新状态", exact: true }).click();
  await service("redis").getByText("正常", { exact: true }).waitFor();
  assert.equal(await page.getByText("状态已过期", { exact: true }).count(), 0);
  assert.notEqual(await checkedTime.getAttribute("datetime"), warningCheckedAt);

  // 推进浏览器时钟触发后台轮询，账号分页结果和当前搜索都应保留。
  await page.getByLabel("搜索账号", { exact: true }).fill("林知夏");
  assert.equal(await users.locator("tbody tr").count(), 1);
  const requestsBeforePoll = healthRequests;
  redisReady = false;
  await page.clock.fastForward(16000);
  await service("redis").getByText("异常", { exact: true }).waitFor();
  assert.ok(healthRequests > requestsBeforePoll, "15 秒后台轮询没有重新读取健康状态");
  assert.equal(await page.getByLabel("搜索账号", { exact: true }).inputValue(), "林知夏");
  assert.equal(await users.locator("tbody tr").count(), 1);
  await page.getByLabel("搜索账号", { exact: true }).fill("");
  assert.equal(await users.locator("tbody tr").count(), 4, "后台健康刷新不应重置账号分页结果");
  redisReady = true;
  await page.getByRole("button", { name: "刷新状态", exact: true }).click();
  await service("redis").getByText("正常", { exact: true }).waitFor();

  empty = true;
  await page.reload();
  await page.getByText("暂无账号。", { exact: true }).waitFor();
  await page.getByText("暂无审计记录。", { exact: true }).waitFor();
  empty = false;
  failStatus = true;
  await page.reload();
  await page.locator(".admin-panel [role=alert]").waitFor();
  await accountRow("admin@example.test").waitFor();
  assert.equal(await users.locator("tbody tr").count(), 3, "健康接口失败不得阻断账号加载");
  assert.equal(await page.locator(".admin-services .admin-badge.good").count(), 0);
  failStatus = false;

  // 页面入口仍由身份与开发模式限制，直接 hash 访问也不能发出管理读取。
  platformAdmin = false;
  const readsBeforeNonAdmin = adminReads.length;
  await page.reload();
  await page.getByText("管理员面板仅限开发模式下的管理员访问。", { exact: true }).waitFor();
  assert.equal(await page.locator(".admin-panel").count(), 0);
  assert.equal(adminReads.length, readsBeforeNonAdmin);
  platformAdmin = true; developmentMode = false;
  const readsBeforeNonDevelopment = adminReads.length;
  await page.reload();
  await page.getByText("管理员面板仅限开发模式下的管理员访问。", { exact: true }).waitFor();
  assert.equal(await page.locator(".admin-panel").count(), 0);
  assert.equal(adminReads.length, readsBeforeNonDevelopment);
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, checks: ["wide-layout", "loading", "loaded-counts", "account-search", "account-pagination", "admin-protection", "account-toggle-csrf", "account-failure-preserves-state", "tool-review-validation", "tool-review-pagination", "tool-review-csrf", "tool-review-failure", "health-refresh", "health-warning", "health-stale-unknown", "health-recovery-timestamp", "health-poll-preserves-search-pagination", "initial-health-failure-keeps-accounts", "empty-state", "error-state", "access-gates", "light-dark", "mobile"], screenshots: directory }));
} finally {
  releaseUsers?.();
  await browser?.close();
  await server.close();
}

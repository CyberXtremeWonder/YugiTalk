import { expect, test, type APIRequestContext } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

let loginState: Awaited<ReturnType<APIRequestContext["storageState"]>>;
test.beforeAll(async ({ request }) => {
  const response = await request.post("/api/v1/auth/login", {
    data: { email: "ui@example.test", password: process.env.E2E_PASSWORD },
    headers: { Origin: "http://127.0.0.1:18080" },
  });
  expect(response.status()).toBe(200);
  loginState = await request.storageState();
});
test.beforeEach(async ({ context }) => {
  await context.addCookies(loginState.cookies);
});

test("只读工具正常执行，助手与任务页不再提供工单入口", async ({ page }) => {
  await page.goto("/#agents");
  await expect(page.getByLabel("创建工单（每次需要审批）")).toHaveCount(0);
  await page.getByLabel("助手名称").fill("可靠任务验收助手");
  await page.getByRole("button", { name: "创建助手", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("版本发布成功");
  await page.getByRole("button", { name: "新建对话" }).click();
  await page
    .getByLabel("选择助手")
    .selectOption({ label: "可靠任务验收助手 · v1 · mock" });
  await page.getByLabel("任务输入").fill("请生成报告：阶段 5 只读工具验收");
  await page.getByRole("button", { name: "发送任务", exact: true }).click();
  await expect(page.locator(".run-status")).toContainText("任务完成", {
    timeout: 30000,
  });
  await expect(page.locator(".process-summary")).toContainText(
    "调用工具：报告排版",
  );
  await expect(page.getByRole("region", { name: "操作审批" })).toHaveCount(0);
  await page.getByRole("button", { name: "任务记录", exact: true }).click();
  await expect(page.locator(".task-list")).toContainText("succeeded");
  await expect(page.getByRole("region", { name: "已创建工单" })).toHaveCount(0);
  await page.getByRole("button", { name: "刷新记录" }).click();
  await expect(page.getByText("我的工单", { exact: true })).toHaveCount(0);
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await page.screenshot({
    path: "../docs/stages/artifacts/stage5-tasks.png",
    fullPage: true,
  });
});

test("手机恢复入口说明费用并携带幂等键和 CSRF；UI 故障状态由接口替身提供", async ({
  page,
}) => {
  const identity = await (await page.request.get("/api/v1/auth/me")).json();
  const conversation = await (
    await page.request.post(
      `/api/v1/workspaces/${identity.workspaces[0].id}/conversations`,
      {
        headers: {
          Origin: "http://127.0.0.1:18080",
          "X-CSRF-Token": identity.csrf_token,
        },
        data: { title: "恢复入口验收" },
      },
    )
  ).json();
  const run = {
    id: crypto.randomUUID(),
    conversation_id: conversation.id,
    status: "failed",
    provider: "mock",
    model: "mock-v1",
    error_code: "model_timeout",
    charged_tokens: 120,
    retryable: true,
    events_url: "/unused",
    input_tokens: null,
    output_tokens: null,
  };
  // 此用例验证界面请求契约；真实恢复、预算和并发领取在后端及容器故障测试验证。
  await page.route("**/api/v1/workspaces/*/runs**", (route) =>
    route.fulfill({ json: { items: [run], next_cursor: null } }),
  );
  await page.route(`**/api/v1/runs/${run.id}/retry`, (route) =>
    route.fulfill({ status: 202, json: { ...run, status: "queued" } }),
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/#tasks");
  await expect(page.locator(".task-retry")).toContainText("可能再次计费");
  await expect(
    page.getByRole("button", { name: "确认恢复任务" }),
  ).toBeEnabled();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await page.screenshot({
    path: "../docs/stages/artifacts/stage5-retry-mobile.png",
    fullPage: true,
  });
  const request = page.waitForRequest(`**/api/v1/runs/${run.id}/retry`);
  await page.getByRole("button", { name: "确认恢复任务" }).click();
  const sent = await request;
  expect(sent.method()).toBe("POST");
  expect(sent.headers()["idempotency-key"]).toMatch(/^[0-9a-f-]{36}$/);
  expect(sent.headers()["x-csrf-token"]).toBe(identity.csrf_token);
});

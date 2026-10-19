import { expect, test, type Page } from "@playwright/test";

/** 使用隔离监控数据检查布局与刷新行为，不连接生产账号或模型。 */
async function setup(page: Page) {
  const sampled = Date.now();
  const times = Array.from({ length: 15 }, (_, index) =>
    new Date(sampled - (14 - index) * 60000).toISOString(),
  );
  const snapshot = {
    sampled_at: new Date(sampled).toISOString(),
    window_minutes: 15,
    http: {
      started_at: new Date(sampled - 3600000).toISOString(),
      requests: 2486,
      server_errors: 8,
      client_errors: 23,
      error_rate: 0.32,
      average_ms: 182.6,
      trend: times.map((timestamp, index) => ({
        timestamp,
        requests: [
          120, 130, 110, 155, 178, 146, 180, 200, 145, 180, 210, 185, 170, 195,
          182,
        ][index],
        server_errors: 0,
        average_ms: 100 + index * 10,
      })),
      routes: [
        {
          method: "POST",
          route: "/api/v1/runs",
          requests: 812,
          average_ms: 245.2,
          max_ms: 812.7,
          client_errors: 12,
          server_errors: 3,
        },
        {
          method: "GET",
          route: "/api/v1/runs/{run_id}/events",
          requests: 704,
          average_ms: 128.4,
          max_ms: 458.2,
          client_errors: 4,
          server_errors: 2,
        },
        {
          method: "GET",
          route: "/api/v1/me/conversations",
          requests: 486,
          average_ms: 42.8,
          max_ms: 96.4,
          client_errors: 3,
          server_errors: 0,
        },
        {
          method: "POST",
          route: "/api/v1/knowledge-bases/{base_id}/documents",
          requests: 484,
          average_ms: 288.3,
          max_ms: 1042.5,
          client_errors: 4,
          server_errors: 3,
        },
      ],
      errors: [
        {
          kind: "http",
          timestamp: times[14],
          status: 503,
          method: "POST",
          route: "/api/v1/runs",
          code: "dependency_unavailable",
          request_id: "test-request-id",
          trace_id: "test-trace-id",
        },
      ],
    },
    tasks: {
      charged_tokens: 1284500,
      recent_tokens: 42680,
      input_tokens: 892600,
      output_tokens: 391900,
      unknown_usage_runs: 2,
      states: {
        queued: 3,
        running: 8,
        waiting_approval: 1,
        waiting_input: 2,
        succeeded: 1240,
        failed: 12,
        cancelled: 5,
      },
      trend: times.map((timestamp, index) => ({
        timestamp,
        tokens: [
          1800, 2100, 1500, 2700, 3200, 2800, 1900, 3500, 4600, 3400, 5100,
          4800, 3900, 5200, 4400,
        ][index],
      })),
      models: [
        {
          provider: "openai",
          model: "configured-model",
          tokens: 980000,
          runs: 958,
        },
        { provider: "mock", model: "mock-v1", tokens: 304500, runs: 313 },
      ],
      errors: [
        {
          kind: "task",
          id: "diagnostic-task",
          code: "model_timeout",
          timestamp: times[13],
        },
        {
          kind: "document",
          id: "diagnostic-document",
          code: "embedding_timeout",
          timestamp: null,
        },
      ],
    },
  };
  let polls = 0;
  let fail = false;
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const body =
      path === "/api/v1/auth/me"
        ? {
            user: {
              id: "admin",
              name: "管理员",
              email: "test@example.test",
              platform_admin: true,
            },
            csrf_token: "test-token",
          }
        : path === "/api/v1/admin/monitoring"
          ? snapshot
          : { items: [], next_cursor: null };
    if (path === "/api/v1/admin/monitoring") {
      polls++;
      if (fail) {
        await route.fulfill({ status: 503, json: { code: "unavailable" } });
        return;
      }
    }
    await route.fulfill({ json: body });
  });
  await page.goto("/#monitoring");
  await expect(
    page.getByRole("heading", { name: "实时监控", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("1,284,500", { exact: true })).toBeVisible();
  return {
    polls: () => polls,
    fail: () => {
      fail = true;
    },
  };
}

test("桌面和手机布局、筛选、刷新与暂停", async ({ page }, info) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  const fixture = await setup(page);
  await expect.poll(fixture.polls, { timeout: 8000 }).toBeGreaterThanOrEqual(2);
  await page.getByRole("button", { name: "暂停刷新" }).click();
  const pausedPolls = fixture.polls();
  await page.waitForTimeout(5500);
  expect(fixture.polls()).toBe(pausedPolls);
  await page.screenshot({ path: info.outputPath("dashboard-desktop.png") });
  await page.getByRole("button", { name: "耗时", exact: true }).click();
  await expect(
    page.getByRole("img", { name: /平均响应耗时趋势/ }),
  ).toBeVisible();
  await page.getByRole("button", { name: "流量", exact: true }).click();
  await page.getByRole("button", { name: "立即刷新" }).click();
  await expect.poll(fixture.polls).toBe(pausedPolls + 1);
  await page.getByLabel("异常类型").selectOption("task");
  await expect(page.getByText("model_timeout", { exact: true })).toBeVisible();
  await expect(
    page.getByText("embedding_timeout", { exact: true }),
  ).toHaveCount(0);
  await page.getByLabel("异常类型").selectOption("all");
  await page.getByText("关联标识", { exact: true }).click();
  await expect(
    page.getByText("test-request-id", { exact: true }),
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    page.getByRole("heading", { name: "实时监控", exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({ path: info.outputPath("dashboard-mobile.png") });
});

test("连接失败保留旧数据并明确标记", async ({ page }) => {
  const fixture = await setup(page);
  fixture.fail();
  await page.getByRole("button", { name: "立即刷新" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "当前展示上次成功获取的数据",
  );
  await expect(page.getByText("1,284,500", { exact: true })).toBeVisible();
  await expect(page.getByText("连接异常", { exact: true })).toBeVisible();
});

test("普通用户无监控入口，直接访问也不加载监控数据", async ({ page }) => {
  let monitoringRequests = 0;
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/admin/monitoring") monitoringRequests++;
    await route.fulfill({
      json:
        path === "/api/v1/auth/me"
          ? {
              user: {
                id: "member",
                name: "普通用户",
                email: "member@example.test",
                platform_admin: false,
              },
              csrf_token: "test-token",
            }
          : { items: [], next_cursor: null },
    });
  });
  await page.goto("/#monitoring");
  await expect(page.getByRole("alert")).toHaveText("实时监控仅限管理员访问。");
  await expect(page.getByRole("button", { name: /实时监控/ })).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "实时监控", exact: true }),
  ).toHaveCount(0);
  expect(monitoringRequests).toBe(0);
});

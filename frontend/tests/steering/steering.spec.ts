import { expect, test } from "@playwright/test";

test("暂停时插话使用原任务，重试复用幂等键，并可选择另开任务", async ({
  page,
}) => {
  const task = {
    id: "run-current",
    status: "paused",
    conversation_id: "conversation",
    message_sequence: 1,
    prompt_preview: "当前任务",
    error_code: null,
    provider: "mock",
    model: "mock",
    charged_tokens: 0,
    input_tokens: null,
    output_tokens: null,
    events_url: "/api/v1/runs/run-current/events",
    event_sequence: 0,
    steering_supported: true,
  };
  const submissions: {
    path: string;
    key: string;
    body: unknown;
    csrf: string;
  }[] = [];
  let failFirst = true;
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST") {
      submissions.push({
        path,
        key: request.headers()["idempotency-key"],
        csrf: request.headers()["x-csrf-token"],
        body: request.postDataJSON(),
      });
      if (path.endsWith("/steer") && failFirst) {
        failFirst = false;
        await route.fulfill({ status: 503, json: {} });
      } else {
        await route.fulfill({ status: 202, json: task });
      }
      return;
    }
    if (path.endsWith("/events")) {
      await route.fulfill({ contentType: "text/event-stream", body: "" });
    } else if (path.endsWith("/me/agents")) {
      await route.fulfill({
        json: {
          items: [
            {
              id: "agent",
              version_id: "version",
              number: 1,
              name: "助手",
              config: { provider: "mock" },
            },
          ],
          next_cursor: null,
        },
      });
    } else if (path.endsWith("/me/skills")) {
      await route.fulfill({ json: { items: [], next_cursor: null } });
    } else if (path.endsWith("/messages")) {
      await route.fulfill({
        json: {
          items: [
            {
              id: "origin",
              sequence: 1,
              role: "user",
              content: "当前任务",
              sources: [],
            },
          ],
          next_cursor: null,
        },
      });
    } else if (path.endsWith("/conversation/runs")) {
      await route.fulfill({ json: { items: [task], next_cursor: null } });
    } else {
      await route.fulfill({ json: task });
    }
  });
  await page.goto("/tests/steering/harness.html");
  await expect(page.getByLabel("消息目标")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "发送插话", exact: true }),
  ).toBeDisabled();
  await page.locator("#prompt").fill("只看国内，用表格回答");
  await page.getByRole("button", { name: "发送插话", exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await page.locator("#prompt").press("Enter");
  await expect(page.locator("#prompt")).toHaveValue("");
  expect(submissions).toHaveLength(2);
  expect(submissions[0].key).toBe(submissions[1].key);
  expect(submissions[1]).toMatchObject({
    path: "/api/v1/runs/run-current/steer",
    csrf: "csrf",
    body: { message: "只看国内，用表格回答" },
  });
  await expect(
    page.getByRole("button", { name: "继续思考", exact: true }).first(),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "停止思考", exact: true }).first(),
  ).toBeVisible();
  await page.getByLabel("消息目标").selectOption("new");
  await page.locator("#prompt").fill("另一个任务");
  await page.getByRole("button", { name: "发送任务", exact: true }).click();
  await expect.poll(() => submissions.length).toBe(3);
  expect(submissions[2].path).toBe("/api/v1/runs");
  await page.screenshot({ path: "../.cache/playwright-steering/steering.png" });
  await page.setViewportSize({ width: 360, height: 800 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: "../.cache/playwright-steering/steering-mobile.png" });
});

test("多个运行任务可明确选择插话目标", async ({ page }) => {
  const submissions: string[] = [];
  const tasks = ["newest", "older"].map((id) => ({
    id,
    status: "running",
    prompt_preview: id,
    provider: "mock",
    model: "mock",
    error_code: null,
    charged_tokens: 0,
    events_url: `/api/v1/runs/${id}/events`,
    event_sequence: 0,
  }));
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === "POST") {
      submissions.push(path);
      await route.fulfill({ status: 202, json: tasks[1] });
    } else if (path.endsWith("/events")) {
      await route.fulfill({ contentType: "text/event-stream", body: "" });
    } else if (path.endsWith("/conversation/runs")) {
      await route.fulfill({ json: { items: tasks, next_cursor: null } });
    } else if (path.includes("/runs/")) {
      await route.fulfill({
        json: tasks.find((task) => path.endsWith(task.id)),
      });
    } else {
      await route.fulfill({ json: { items: [], next_cursor: null } });
    }
  });
  await page.goto("/tests/steering/harness.html");
  await page.getByLabel("消息目标").selectOption("older");
  await page.locator("#prompt").fill("修改旧任务");
  await page.locator("#prompt").press("Enter");
  await expect.poll(() => submissions).toEqual(["/api/v1/runs/older/steer"]);
});

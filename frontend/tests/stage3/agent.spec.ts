import { expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";
const password = process.env.E2E_PASSWORD!;

test("发布助手、多轮工具对话、历史恢复、移动端与停止", async ({ page }) => {
  test.setTimeout(60000);
  await page.goto("/");
  await page.getByLabel("邮箱").fill("ui@example.test");
  await page.getByLabel("密码", { exact: true }).fill(password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByRole("button", { name: "Agent", exact: true }).click();
  await page.getByLabel("助手名称").fill("验收助手");
  await page.getByRole("button", { name: "创建助手", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("版本发布成功");
  await page.getByRole("button", { name: "新建对话" }).click();
  await page.getByLabel("任务输入").fill("请统计这段文本：你好世界");
  await expect(
    page.getByRole("button", { name: "发送任务", exact: true }),
  ).toHaveCSS("cursor", "pointer");
  await page.getByRole("button", { name: "发送任务", exact: true }).click();
  await expect(page.locator(".run-status")).toContainText("任务完成", {
    timeout: 30000,
  });
  await expect(page.locator(".message.assistant pre")).toContainText(
    "characters",
  );
  await expect(page.locator(".process-summary")).toContainText(
    "调用工具：文本统计",
  );
  await page.locator(".message-list").screenshot({
    path: "../docs/stages/artifacts/stage3-process-summary.png",
  });
  await page.locator(".process-summary summary").click();
  await expect(page.locator(".process-summary ol")).toBeHidden();
  await page.locator(".process-summary summary").click();
  await page.getByLabel("任务输入").fill("这是第二轮对话");
  await page.getByLabel("任务输入").press("Enter");
  await expect(page.locator(".message.assistant")).toHaveCount(2, {
    timeout: 30000,
  });
  await expect(page.locator(".message.user")).toHaveCount(2);
  await page.reload();
  await page.locator(".history button").first().click();
  await expect(page.locator(".message.assistant")).toHaveCount(2);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByLabel("任务输入")).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
  await page.getByLabel("任务输入").fill("长文本".repeat(300));
  await page.getByRole("button", { name: "发送任务", exact: true }).click();
  await expect(page.locator(".stop-task .stop-square")).toBeVisible();
  await page.getByRole("button", { name: "停止生成" }).click();
  await expect(page.locator(".run-status")).toContainText("任务已结束", {
    timeout: 30000,
  });
  await expect(page.getByRole("alert")).toContainText("cancelled");
  await page.screenshot({
    path: "../docs/stages/artifacts/stage3-chat-mobile.png",
    fullPage: true,
  });
  // 终态可能先于页面连接 SSE，仍必须从数据库状态显示失败原因。
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.getByRole("button", { name: "Agent", exact: true }).click();
  await page.getByRole("button", { name: /验收助手.*版本 1/ }).click();
  await page.getByLabel("单次任务预算（保守额度）").fill("512");
  await page.getByRole("button", { name: "发布新版本", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("版本发布成功");
  await page.getByRole("button", { name: "新建对话" }).click();
  await page.getByLabel("任务输入").fill("预算耗尽演示");
  await page.getByRole("button", { name: "发送任务", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText(
    "token_budget_exhausted",
    { timeout: 30000 },
  );
});

test("真实 PostgreSQL 并发任务限制、幂等和取消结算", async ({ request }) => {
  const login = await request.post("/api/v1/auth/login", {
    headers: { Origin: "http://127.0.0.1:18080" },
    data: { email: "ui@example.test", password },
  });
  expect(login.status()).toBe(200);
  const identity = await login.json();
  const headers = {
    Origin: "http://127.0.0.1:18080",
    "X-CSRF-Token": identity.csrf_token,
  };
  const space = identity.workspaces[0].id;
  // 并发用独立助手，避免上一个测试故意发布的低预算版本影响并发结果。
  const agent = await request.post(`/api/v1/workspaces/${space}/agents`, {
    headers,
    data: { name: "并发助手", token_budget: 30000 },
  });
  expect(agent.status()).toBe(201);
  const version = (await agent.json()).version_id;
  const conversations = await Promise.all(
    [1, 2, 3].map(
      async (index) =>
        (
          await (
            await request.post(`/api/v1/workspaces/${space}/conversations`, {
              headers,
              data: { title: `并发任务 ${index}` },
            })
          ).json()
        ).id,
    ),
  );
  const submissions = conversations.map((id) => ({
    headers: { ...headers, "Idempotency-Key": randomUUID() },
    data: {
      conversation_id: id,
      agent_version_id: version,
      message: "并发取消验收".repeat(400),
    },
  }));
  const responses = await Promise.all(
    submissions.map((submission) => request.post("/api/v1/runs", submission)),
  );
  expect(responses.map((response) => response.status()).sort()).toEqual([
    202, 202, 429,
  ]);
  for (let index = 0; index < responses.length; index++) {
    if (responses[index].status() !== 202) continue;
    const run = await responses[index].json();
    const replay = await request.post("/api/v1/runs", submissions[index]);
    expect((await replay.json()).id).toBe(run.id);
    const cancel = await request.post(`/api/v1/runs/${run.id}/cancel`, {
      headers,
    });
    expect((await cancel.json()).status).toBe("cancelled");
    const stream = await request.get(run.events_url);
    expect((await stream.text()).match(/event: error/g)).toHaveLength(1);
  }
});

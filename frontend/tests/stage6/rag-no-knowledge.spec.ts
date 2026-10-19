import { expect, test } from "@playwright/test";

test.use({ baseURL: "http://127.0.0.1:8080", ignoreHTTPSErrors: false });

test("空知识库固定拒答且刷新后保留，不展示模型通用答案或来源", async ({
  page,
}) => {
  test.skip(
    !process.env.E2E_EMPTY_RAG_EMAIL,
    "请通过 run-rag-no-knowledge-ui.py 创建专用空库验收身份",
  );
  await page.goto("/");
  await page
    .getByLabel("邮箱", { exact: true })
    .fill(process.env.E2E_EMPTY_RAG_EMAIL!);
  await page
    .getByLabel("密码", { exact: true })
    .fill(process.env.E2E_EMPTY_RAG_PASSWORD!);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.locator("#prompt")).toBeVisible();
  const identity = await (await page.request.get("/api/v1/auth/me")).json();
  const headers = {
    Origin: "http://127.0.0.1:8080",
    "X-CSRF-Token": identity.csrf_token,
  };
  const space = identity.workspaces[0].id;
  const agent = await page.request.post(`/api/v1/workspaces/${space}/agents`, {
    headers,
    data: { name: "空知识库拒答验收", tools: [], token_budget: 100000 },
  });
  expect(agent.ok()).toBeTruthy();
  const created = await page.request.post(
    `/api/v1/workspaces/${space}/knowledge-bases`,
    {
      headers,
      data: { name: "空知识库拒答验收" },
    },
  );
  expect(created.ok()).toBeTruthy();
  const base = (await created.json()).id;
  await page.reload();
  await expect(page.locator("#prompt")).toBeVisible();
  await page.getByLabel("对话知识库", { exact: true }).selectOption(base);
  await page.locator("#prompt").fill("你知道薛定谔的猫吗");
  await page.getByRole("button", { name: "发送任务", exact: true }).click();
  await expect(page.getByText("没有相关知识", { exact: true })).toBeVisible();
  const runs = await (
    await page.request.get(`/api/v1/workspaces/${space}/runs`)
  ).json();
  expect(runs.items).toHaveLength(1);
  expect(runs.items[0]).toMatchObject({
    status: "succeeded",
    error_code: null,
    charged_tokens: 0,
  });
  const messages = await (
    await page.request.get(
      `/api/v1/conversations/${runs.items[0].conversation_id}/messages`,
    )
  ).json();
  expect(
    messages.items.filter((row: { role: string }) => row.role === "assistant"),
  ).toMatchObject([{ content: "没有相关知识", sources: [] }]);
  await expect(page.locator(".source-list")).toHaveCount(0);
  await page.reload();
  await page
    .getByRole("button", { name: "你知道薛定谔的猫吗", exact: true })
    .click();
  await expect(page.getByText("没有相关知识", { exact: true })).toBeVisible();
  await page
    .getByText("没有相关知识", { exact: true })
    .scrollIntoViewIfNeeded();
  await expect(
    page.getByText("没有相关知识", { exact: true }),
  ).toBeInViewport();
  await page.screenshot({
    path: "../docs/stages/artifacts/rag-no-knowledge-ui.png",
  });
});

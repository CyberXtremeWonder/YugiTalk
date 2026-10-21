import { expect, test } from "@playwright/test";

test.use({ baseURL: "http://127.0.0.1:8080", ignoreHTTPSErrors: false });

test("自动检索空知识库后说明证据不足，刷新后保留且无来源", async ({ page }) => {
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
  const agent = await page.request.post("/api/v1/me/agents", {
    headers,
    data: { name: "空知识库拒答验收", tools: [], token_budget: 100000 },
  });
  expect(agent.ok()).toBeTruthy();
  const created = await page.request.post("/api/v1/me/knowledge-bases", {
    headers,
    data: { name: "空知识库拒答验收" },
  });
  expect(created.ok()).toBeTruthy();
  await page.reload();
  await expect(page.locator("#prompt")).toBeVisible();
  await expect(page.getByLabel("对话知识库", { exact: true })).toHaveCount(0);
  await page
    .getByLabel("选择助手")
    .selectOption((await agent.json()).version_id);
  const question = "根据空知识库拒答验收知识库，说明薛定谔的猫。";
  const answer = "没有找到充分的知识库证据，请补充相关资料或问题。";
  await page.locator("#prompt").fill(question);
  await page.getByRole("button", { name: "发送任务", exact: true }).click();
  await expect(page.getByText(answer, { exact: true })).toBeVisible();
  const runs = await (await page.request.get("/api/v1/me/runs")).json();
  expect(runs.items).toHaveLength(1);
  expect(runs.items[0]).toMatchObject({
    status: "succeeded",
    error_code: null,
  });
  expect(runs.items[0].charged_tokens).toBeGreaterThan(0);
  const messages = await (
    await page.request.get(
      `/api/v1/conversations/${runs.items[0].conversation_id}/messages`,
    )
  ).json();
  expect(
    messages.items.filter((row: { role: string }) => row.role === "assistant"),
  ).toMatchObject([{ content: answer, sources: [] }]);
  await expect(page.locator(".source-list")).toHaveCount(0);
  await page.reload();
  await page.getByRole("button", { name: question, exact: true }).click();
  await expect(page.getByText(answer, { exact: true })).toBeVisible();
  await page.getByText(answer, { exact: true }).scrollIntoViewIfNeeded();
  await expect(page.getByText(answer, { exact: true })).toBeInViewport();
  await page.screenshot({
    path: "../docs/stages/artifacts/rag-no-knowledge-ui.png",
  });
});

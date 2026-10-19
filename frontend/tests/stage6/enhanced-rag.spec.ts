import { expect, test } from "@playwright/test";

test("增强 RAG 在同一任务中澄清恢复并核验来源", async ({ page }) => {
  test.skip(
    !process.env.E2E_RAG_EMAIL,
    "请通过 run-enhanced-rag-ui.py 初始化独立 RAG 验收账号",
  );
  test.setTimeout(120000);
  const nonce = crypto.randomUUID().slice(0, 8);
  const email = process.env.E2E_RAG_EMAIL!;
  const password = process.env.E2E_PASSWORD!;
  await page.goto("/");
  await page.getByLabel("邮箱", { exact: true }).fill(email);
  await page.getByLabel("密码", { exact: true }).fill(password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.locator("#prompt")).toBeVisible();
  const identity = await (await page.request.get("/api/v1/auth/me")).json();
  const headers = {
    Origin: "https://localhost:18443",
    "X-CSRF-Token": identity.csrf_token,
  };
  const space = identity.workspaces[0].id;
  const agent = await page.request.post(`/api/v1/workspaces/${space}/agents`, {
    headers,
    data: { name: "增强 RAG 验收", tools: [], token_budget: 100000 },
  });
  expect(agent.ok()).toBeTruthy();
  const created = await page.request.post(
    `/api/v1/workspaces/${space}/knowledge-bases`,
    { headers, data: { name: `RAG-${nonce}` } },
  );
  expect(created.ok()).toBeTruthy();
  const base = (await created.json()).id;
  const uploaded = await page.request.post(
    `/api/v1/knowledge-bases/${base}/documents?filename=rag-ui.md`,
    {
      headers: { ...headers, "Content-Type": "application/octet-stream" },
      data: Buffer.from("Milvus 保存 BGE-M3 向量，PostgreSQL 保存资料元数据。"),
    },
  );
  expect(uploaded.ok()).toBeTruthy();
  const document = (await uploaded.json()).id;
  await expect
    .poll(
      async () => {
        const result = await (
          await page.request.get(`/api/v1/knowledge-bases/${base}/documents`)
        ).json();
        return result.items.find(
          (row: { id: string; status: string }) => row.id === document,
        )?.status;
      },
      { timeout: 60000 },
    )
    .toBe("ready");
  await page.reload();
  await expect(page.locator("#prompt")).toBeVisible();
  await page.getByLabel("对话知识库", { exact: true }).selectOption(base);
  await page.locator("#prompt").fill("它的 Milvus 用途是什么？");
  await page.getByRole("button", { name: "发送任务", exact: true }).click();
  await expect(page.getByLabel("补充信息", { exact: true })).toBeVisible({
    timeout: 30000,
  });
  const runsBefore = await (
    await page.request.get(`/api/v1/workspaces/${space}/runs`)
  ).json();
  const task = runsBefore.items.find(
    (row: { status: string }) => row.status === "waiting_input",
  );
  expect(task).toBeTruthy();
  await page.reload();
  await page
    .getByRole("button", { name: "它的 Milvus 用途是什么？", exact: true })
    .click();
  await expect(page.getByLabel("补充信息", { exact: true })).toBeVisible();
  await page.locator("#prompt").fill("指的是 Milvus，它保存 BGE-M3 向量。");
  await page.getByRole("button", { name: "发送任务", exact: true }).click();
  await expect
    .poll(
      async () =>
        (await (await page.request.get(`/api/v1/runs/${task.id}`)).json())
          .status,
      { timeout: 30000 },
    )
    .toBe("succeeded");
  await expect(
    page.getByText("任务完成", { exact: false }).first(),
  ).toBeVisible();
  await page.locator(".source-list summary").last().click();
  await expect(page.locator(".source-list small").last()).toContainText("E-");
  await expect(page.locator(".source-list small").last()).toBeVisible();
  await page.locator(".source-list button").last().click();
  await expect(page.locator(".source-preview").last()).toContainText("Milvus");
  await expect(page.locator(".source-preview").last()).toBeVisible();
  const runsAfter = await (
    await page.request.get(`/api/v1/workspaces/${space}/runs`)
  ).json();
  expect(runsAfter.items.map((row: { id: string }) => row.id)).toEqual([
    task.id,
  ]);
  await page.screenshot({
    path: "../docs/stages/artifacts/enhanced-rag-ui.png",
  });
  expect(
    (
      await page.request.delete(`/api/v1/documents/${document}`, { headers })
    ).ok(),
  ).toBeTruthy();
});

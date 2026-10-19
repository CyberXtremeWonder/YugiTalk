import { expect, test, type APIRequestContext } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

let loginState: Awaited<ReturnType<APIRequestContext["storageState"]>>;
// 同一组旅程复用登录会话，避免重复登录触发真实安全限流。
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

test("真实 BGE-M3 导入、去重、检索、引用与删除传播", async ({ page }) => {
  test.setTimeout(120000);
  await page.goto("/#agents");
  await page.getByLabel("助手名称").fill("知识问答验收");
  await page.getByRole("button", { name: "创建助手", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("版本发布成功");
  await page.getByRole("button", { name: "知识库", exact: true }).click();
  await page.getByLabel("知识库名称").fill("验收知识库");
  await page.getByRole("button", { name: "创建知识库", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("知识库已创建");
  const upload = page.getByLabel("上传资料（最多 20 MB）");
  const file = {
    name: "部署指南.md",
    mimeType: "text/markdown",
    buffer: Buffer.from(
      "YugiTalk 项目使用 Milvus 保存向量，PostgreSQL 保存用户和文档元数据。部署时执行 docker compose up -d，API 和 Worker 使用同一个后端镜像。",
    ),
  };
  await upload.setInputFiles(file);
  const doc = page
    .locator(".document-list > li")
    .filter({ hasText: "部署指南.md" });
  await expect(doc).toContainText("可用", { timeout: 60000 });
  await expect(doc.locator(".import-progress")).toHaveCount(0);
  await upload.setInputFiles(file);
  await expect(page.getByRole("status")).toContainText("上传完成");
  await expect(page.locator(".document-list > li")).toHaveCount(1);
  await page.getByLabel("检索问题").fill("向量数据保存在哪个数据库？");
  await page.getByRole("button", { name: "检索资料" }).click();
  await expect(page.locator(".source-list")).toContainText("部署指南.md");
  await page.locator(".source-list summary").click();
  await page.locator(".source-list button").first().click();
  await expect(page.locator(".source-preview")).toContainText("Milvus");
  await page.getByRole("button", { name: "新建对话" }).click();
  await page
    .getByLabel("选择助手")
    .selectOption({ label: "知识问答验收 · v1 · mock" });
  await page.getByLabel("对话知识库").selectOption({ label: "验收知识库" });
  await page.getByLabel("任务输入").fill("向量数据保存在哪个数据库？");
  await page.getByRole("button", { name: "发送任务", exact: true }).click();
  await expect(page.locator(".run-status")).toContainText("任务完成", {
    timeout: 60000,
  });
  await expect(page.locator(".message.assistant")).toContainText("Milvus");
  await expect(page.locator(".message.assistant .source-list")).toContainText(
    "部署指南.md",
  );
  await page
    .locator(".message.assistant .source-list")
    .scrollIntoViewIfNeeded();
  await page.screenshot({
    path: "../docs/stages/artifacts/stage4-rag-chat.png",
  });
  await page.reload();
  await page
    .locator(".history")
    .getByRole("button", { name: "向量数据保存在哪个数据库？", exact: true })
    .click();
  await expect(page.locator(".message.assistant .source-list")).toContainText(
    "部署指南.md",
  );
  await page.goto("/#knowledge");
  await doc.getByRole("button", { name: "删除", exact: true }).click();
  await expect(doc).toHaveCount(0);
  await page.reload();
  await expect(
    page.locator(".document-list > li").filter({ hasText: "部署指南.md" }),
  ).toHaveCount(0);
  await page.getByLabel("检索问题").fill("向量数据保存在哪个数据库？");
  await page.getByRole("button", { name: "检索资料" }).click();
  await expect(page.getByRole("status")).toContainText("未找到足够相关证据");
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  expect(
    (await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze())
      .violations,
  ).toEqual([]);
});

test("导入失败显示具体步骤，重试开启新版本流程", async ({ page }) => {
  await page.goto("/#knowledge");
  await page.getByLabel("知识库名称").fill("失败流程验收");
  await page.getByRole("button", { name: "创建知识库", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("知识库已创建");
  await page.getByLabel("上传资料（最多 20 MB）").setInputFiles({
    name: "损坏文件.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("%PDF-broken"),
  });
  const doc = page
    .locator(".document-list > li")
    .filter({ hasText: "损坏文件.pdf" });
  const flow = page.getByLabel("文件流程：损坏文件.pdf", { exact: true });
  await expect(flow.locator(".import-error")).toContainText(
    "失败步骤：解析与分块",
    { timeout: 30000 },
  );
  await expect(flow.locator(".import-steps [data-state=failed]")).toContainText(
    "解析与分块",
  );
  await expect(flow.locator(".import-error")).toContainText("PDF 文件格式损坏");
  await doc.getByRole("button", { name: "重试", exact: true }).click();
  await expect(doc).toContainText("v2", { timeout: 30000 });
  await expect(flow.locator(".import-error")).toContainText(
    "失败步骤：解析与分块",
    { timeout: 30000 },
  );
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await flow.scrollIntoViewIfNeeded();
  await page.screenshot({
    path: "../docs/stages/artifacts/stage4-upload-error.png",
  });
  expect(
    (await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze())
      .violations,
  ).toEqual([]);
});

test("个人偏好确认、修改、关闭和删除，手机布局", async ({ page }) => {
  await page.goto("/#settings");
  const panel = page.locator(".memory-settings");
  await panel.getByLabel("偏好内容").fill("默认使用中文，示例优先 Python。");
  await expect(panel.getByRole("button", { name: "保存偏好" })).toBeDisabled();
  await panel.getByLabel("我确认保存此偏好，内容不含密码、令牌或密钥").check();
  await panel.getByRole("button", { name: "保存偏好" }).click();
  await expect(panel.locator(".document-list")).toContainText("默认使用中文");
  await panel.getByRole("button", { name: "修改", exact: true }).click();
  await panel.getByLabel("偏好内容").fill("回答简洁，示例优先 Python。");
  await panel.getByLabel("我确认保存此偏好，内容不含密码、令牌或密钥").check();
  await panel.getByRole("button", { name: "更新偏好" }).click();
  await expect(panel.locator(".document-list")).toContainText("回答简洁");
  await panel.getByLabel("启用长期记忆").uncheck();
  await expect(panel.getByLabel("偏好内容")).toBeHidden();
  await page.reload();
  await expect(panel.getByLabel("启用长期记忆")).not.toBeChecked();
  await panel.getByLabel("启用长期记忆").check();
  await expect(panel.locator(".document-list")).toContainText("回答简洁");
  await panel.getByRole("button", { name: "删除", exact: true }).click();
  await expect(panel.locator(".document-list > li")).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "../docs/stages/artifacts/stage4-memory-mobile.png",
  });
});

test("递归文件夹上传、去重与竖向流程", async ({ page }, testInfo) => {
  test.setTimeout(120000);
  const folder = testInfo.outputPath("验收资料");
  await mkdir(path.join(folder, "子目录"), { recursive: true });
  await writeFile(
    path.join(folder, "部署.md"),
    "文件夹中的部署资料使用 Docker Compose。",
  );
  await writeFile(
    path.join(folder, "子目录", "向量.txt"),
    "文件夹中的向量资料使用 Milvus 和 BGE-M3。",
  );
  await writeFile(path.join(folder, "图片.jpg"), "不支持的格式应跳过");
  await writeFile(path.join(folder, "子目录", "空文件.txt"), "");
  await writeFile(path.join(folder, "损坏文件.pdf"), "%PDF-broken");
  await page.goto("/#knowledge");
  await page.getByLabel("知识库名称").fill("文件夹验收");
  await page.getByRole("button", { name: "创建知识库", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("知识库已创建");
  await page.getByLabel("上传文件夹", { exact: true }).setInputFiles(folder);
  const batch = page.getByLabel("批量上传进度");
  await expect(batch).toContainText("上传进度：4/4", { timeout: 30000 });
  await expect(batch).toContainText("成功 3");
  await expect(batch).toContainText("失败 1");
  await expect(batch).toContainText("跳过 1");
  await expect(batch).toContainText("空文件.txt");
  const docs = page.locator(".document-list > li");
  await expect(docs).toHaveCount(3);
  await expect(docs.filter({ hasText: "部署.md" })).toContainText("可用", {
    timeout: 60000,
  });
  await expect(docs.filter({ hasText: "向量.txt" })).toContainText("可用", {
    timeout: 60000,
  });
  await page.getByLabel("上传文件夹", { exact: true }).setInputFiles(folder);
  await expect(page.getByRole("status")).toContainText("上传完成");
  await expect(docs).toHaveCount(3);
  const failed = page.getByLabel("文件流程：损坏文件.pdf", { exact: true });
  await expect(failed.locator(".import-error")).toContainText(
    "失败步骤：解析与分块",
  );
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    const left = await page
      .getByRole("region", { name: "上传流程", exact: true })
      .boundingBox();
    const right = await page
      .getByRole("region", { name: "知识库文件", exact: true })
      .boundingBox();
    expect(left).not.toBeNull();
    expect(right).not.toBeNull();
    if (width === 1440)
      expect(left!.x + left!.width).toBeLessThanOrEqual(right!.x);
    else expect(left!.y + left!.height).toBeLessThanOrEqual(right!.y);
    const steps = failed.locator(".import-steps li");
    const boxes = await steps.evaluateAll((nodes) =>
      nodes.map((node) => ({
        x: node.getBoundingClientRect().x,
        y: node.getBoundingClientRect().y,
      })),
    );
    expect(boxes).toHaveLength(7);
    for (let i = 1; i < boxes.length; i++) {
      expect(boxes[i].x).toBeCloseTo(boxes[0].x, 0);
      expect(boxes[i].y).toBeGreaterThan(boxes[i - 1].y);
    }
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  }
  await failed.scrollIntoViewIfNeeded();
  await page.screenshot({
    path: "../docs/stages/artifacts/stage4-folder-upload.png",
  });
  expect(
    (await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze())
      .violations,
  ).toEqual([]);
});

test("整批成功后重置流程，下次上传从零展示", async ({ page }, testInfo) => {
  test.setTimeout(120000);
  const folder = testInfo.outputPath("成功批次");
  await mkdir(folder, { recursive: true });
  await writeFile(
    path.join(folder, "一.txt"),
    "第一份资料使用 Milvus 保存向量。",
  );
  await writeFile(
    path.join(folder, "二.md"),
    "第二份资料使用 PostgreSQL 保存元数据。",
  );
  await page.goto("/#knowledge");
  await page.getByLabel("知识库名称").fill("重置流程验收");
  await page.getByRole("button", { name: "创建知识库", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("知识库已创建");
  await page.getByLabel("上传文件夹", { exact: true }).setInputFiles(folder);
  const docs = page.locator(".document-list > li");
  await expect(docs).toHaveCount(2);
  for (const name of ["一.txt", "二.md"]) {
    await expect(docs.filter({ hasText: name })).toContainText("可用", {
      timeout: 60000,
    });
  }
  await expect(page.getByLabel("批量上传进度")).toHaveCount(0);
  await expect(page.locator(".import-progress")).toHaveCount(0);

  // 只暂停一次上传请求，核验新批次从 0 开始，随后仍由真实服务导入。
  /** 释放测试中被暂停的上传响应，验证异步流程显示。 */
  let releaseUpload = () => {};
  const gate = new Promise<void>((resolve) => {
    releaseUpload = resolve;
  });
  await page.route(/\/documents\?filename=reset\.md$/, async (route) => {
    await gate;
    await route.continue();
  });
  await page.getByLabel("上传资料（最多 20 MB）").setInputFiles({
    name: "reset.md",
    mimeType: "text/markdown",
    buffer: Buffer.from("新批次资料独立建立索引。"),
  });
  try {
    await expect(page.getByLabel("批量上传进度")).toContainText(
      "上传进度：0/1",
    );
  } finally {
    releaseUpload();
  }
  await expect(docs.filter({ hasText: "reset.md" })).toContainText("可用", {
    timeout: 60000,
  });
  await expect(docs).toHaveCount(3);
  await expect(page.getByLabel("批量上传进度")).toHaveCount(0);
  await expect(page.locator(".import-progress")).toHaveCount(0);
});

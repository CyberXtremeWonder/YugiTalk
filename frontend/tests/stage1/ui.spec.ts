import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

for (const width of [360, 768, 1440]) {
  test(`${width}px 布局、主题、输入焦点与可访问性`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("/");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(
      "今天，一起完成什么？",
    );
    await page.locator("#prompt").click();
    expect(
      await page
        .locator("#prompt")
        .evaluate((element) => getComputedStyle(element).outlineStyle),
    ).toBe("none");
    expect(
      await page.evaluate(
        () =>
          document.documentElement.scrollWidth <= innerWidth &&
          document.documentElement.scrollHeight <= innerHeight,
      ),
    ).toBe(true);
    expect(
      (await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze())
        .violations,
    ).toEqual([]);
    await page.screenshot({
      path: `../docs/stages/artifacts/stage1-light-${width}.png`,
    });
    if (width <= 640)
      await page.getByRole("button", { name: "导航", exact: true }).click();
    await page.getByRole("button", { name: "切换到深色主题" }).click();
    if (width <= 640)
      await page.getByRole("button", { name: "导航", exact: true }).click();
    expect(
      (await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze())
        .violations,
    ).toEqual([]);
    await expect(
      page.getByRole("button", { name: "发送任务，当前阶段尚未连接模型" }),
    ).toBeDisabled();
  });
}
test("视图切换、任务草稿与模态抽屉", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "报告生成", exact: true }).click();
  await expect(page.locator("#prompt")).toHaveValue(
    "根据提供的资料，生成一份结构清晰的报告。",
  );
  await page.getByRole("button", { name: "Agent", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Agent");
  await page.getByRole("button", { name: "新建对话" }).click();
  await expect(page.locator("#prompt")).toHaveValue("");
  const trigger = page.getByRole("button", { name: "展开详情" });
  await trigger.click();
  await expect(page.getByRole("dialog")).toBeVisible();
  expect(
    (await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze())
      .violations,
  ).toEqual([]);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeHidden();
  await expect(trigger).toBeFocused();
});
test("浏览器通过同源代理访问真实后端", async ({ page }) => {
  await page.goto("/");
  const live = await page.request.get("/health/live");
  expect(live.status()).toBe(200);
  await page.getByRole("button", { name: "设置", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("API：ready");
});

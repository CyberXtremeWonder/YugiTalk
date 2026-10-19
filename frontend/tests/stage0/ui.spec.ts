import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

for (const width of [360, 768, 1440]) {
  test(`草图 ${width}px：布局、主题与可访问性`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("/docs/ui/workbench.html");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await expect(
      page.getByRole("link", {
        name: width <= 640 ? "YugiTalk 手机首页" : "YugiTalk 首页",
        exact: true,
      }),
    ).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollHeight <= innerHeight,
      ),
    ).toBe(true);
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    await page.screenshot({
      path: `../docs/stages/artifacts/ui-light-${width}.png`,
      fullPage: true,
    });

    const lightAudit = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa"])
      .analyze();
    expect(lightAudit.violations).toEqual([]);

    if (width <= 640)
      await page.getByRole("button", { name: "导航", exact: true }).click();
    await page.getByRole("button", { name: "切换到深色主题" }).click();
    if (width <= 640)
      await page.getByRole("button", { name: "导航", exact: true }).click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    const darkAudit = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa"])
      .analyze();
    expect(darkAudit.violations).toEqual([]);
    await page.screenshot({
      path: `../docs/stages/artifacts/ui-dark-${width}.png`,
      fullPage: true,
    });

    if (width <= 640)
      await page.getByRole("button", { name: "导航", exact: true }).click();
    await page.getByRole("button", { name: "展开详情" }).click();
    await expect(page.locator("#inspector")).toBeVisible();
    await page
      .getByRole("button", { name: "关闭任务详情", exact: true })
      .click();
    await expect(page.locator("#inspector")).toBeHidden();
    await expect(
      page.getByRole("button", { name: "发送任务，草图中暂不可用" }),
    ).toBeDisabled();
  });
}

test("手机导航与键盘操作", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 900 });
  await page.goto("/docs/ui/workbench.html");
  const menu = page.getByRole("button", { name: "导航", exact: true });
  await menu.focus();
  await page.keyboard.press("Enter");
  await expect(menu).toHaveAttribute("aria-expanded", "true");
  await expect(
    page.getByRole("navigation", { name: "主要导航" }),
  ).toBeVisible();
  await page.keyboard.press("Enter");
  await expect(menu).toHaveAttribute("aria-expanded", "false");
});

test("窄屏任务抽屉的焦点、Escape 与视口切换", async ({ page }) => {
  await page.setViewportSize({ width: 768, height: 1000 });
  await page.goto("/docs/ui/workbench.html");
  const trigger = page.getByRole("button", { name: "展开详情" });
  await trigger.click();
  const dialog = page.getByRole("dialog", { name: "任务详情示例" });
  await expect(dialog).toBeVisible();
  await expect(
    page.getByRole("button", { name: "关闭任务详情", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(
    dialog.getByRole("link", { name: "项目需求文档" }),
  ).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("button", { name: "关闭任务详情", exact: true }),
  ).toBeFocused();
  const drawerAudit = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa"])
    .analyze();
  expect(drawerAudit.violations).toEqual([]);
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused();
  await page.setViewportSize({ width: 1440, height: 1000 });
  await expect(page.locator("#inspector")).toBeHidden();
  expect(await page.evaluate(() => document.body.style.overflow)).toBe("");
});

test("左侧入口切换独立视图，新建对话与任务灵感", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/docs/ui/workbench.html");
  for (const [name, title] of [
    ["Agent", "Agent"],
    ["知识库", "知识库"],
    ["任务记录", "任务记录"],
    ["设置", "设置"],
  ]) {
    await page.getByRole("button", { name, exact: true }).click();
    await expect(
      page.getByRole("heading", { level: 1, name: title, exact: true }),
    ).toBeVisible();
    await expect(page.locator("#chat-view")).toBeHidden();
  }
  await page.getByRole("button", { name: "新建对话" }).click();
  await expect(page.locator("#secondary-view")).toBeHidden();
  await expect(page.locator("#prompt")).toHaveValue("");
  await page.getByRole("button", { name: "报告生成", exact: true }).click();
  await expect(page.locator("#prompt")).toHaveValue(
    "根据提供的资料，生成一份结构清晰的报告。",
  );
  await expect(page.locator("#prompt")).toBeFocused();
  await page.getByRole("button", { name: "新建对话" }).click();
  await expect(page.locator("#prompt")).toHaveValue("");
  const logo = await page
    .getByRole("link", { name: "YugiTalk 首页" })
    .boundingBox();
  expect(logo?.x).toBeLessThan(100);
  expect(logo?.y).toBeLessThan(100);
});

test("减少动态效果设置", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/docs/ui/workbench.html");
  const duration = await page
    .locator("#theme")
    .evaluate((button) => getComputedStyle(button).transitionDuration);
  expect(duration).toBe("0s");
});

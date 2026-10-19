import { expect, test } from "@playwright/test";

test("HTTPS生产工作台能登录，安全策略不破坏页面功能", async ({ page }) => {
  const violations: string[] = [];
  page.on("pageerror", (error) => violations.push(error.name));
  await page.goto("/");
  await page.getByLabel("邮箱", { exact: true }).fill("stage6@example.test");
  // 超过原 128 位的输入必须完整提交，错误凭证由认证结果判断。
  const longPassword = "长度验收_".repeat(64);
  const passwordInput = page.getByLabel("密码", { exact: true });
  await passwordInput.fill(longPassword);
  await expect(passwordInput).toHaveValue(longPassword);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveText("邮箱或密码不正确");
  await page
    .getByLabel("密码", { exact: true })
    .fill(process.env.E2E_PASSWORD!);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.locator("#prompt")).toBeVisible();
  expect(
    (await page.context().cookies()).find(
      (cookie) => cookie.name === "yugitalk_session",
    )?.secure,
  ).toBe(true);
  await page.reload();
  await expect(page.locator("#prompt")).toBeVisible();
  expect(violations).toEqual([]);
  await page.screenshot({
    path: "../docs/stages/artifacts/stage6-https-workbench.png",
  });
});

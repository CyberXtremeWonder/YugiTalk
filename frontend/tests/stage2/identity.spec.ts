import {
  expect,
  test,
  type Page,
  type APIRequestContext,
} from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const origin = "http://127.0.0.1:18080";
const owner = "owner@example.test";
const member = "member@example.test";
const password = process.env.E2E_PASSWORD!;
if (!password) throw new Error("请通过阶段 2 验收脚本生成隔离测试账号");

/** 为浏览器测试填写登录表单，并等待已认证页面出现。 */
async function login(page: Page) {
  await page.goto("/");
  await page.getByLabel("邮箱", { exact: true }).fill(owner);
  await page.getByLabel("密码", { exact: true }).fill(password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.locator("#prompt")).toBeVisible();
}
/** 为隔离浏览器测试取得会话与防伪令牌。 */
async function authenticate(request: APIRequestContext, email: string) {
  const response = await request.post("/api/v1/auth/login", {
    data: { email, password },
    headers: { Origin: origin },
  });
  expect(response.status()).toBe(200);
  return response.json();
}

test("登录错误、刷新保留会话、退出后令牌失效", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("邮箱", { exact: true }).fill(owner);
  await page.getByLabel("密码", { exact: true }).fill("wrong-password");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveText("邮箱或密码不正确");
  await expect(page.getByLabel("密码", { exact: true })).toHaveValue("");
  await login(page);
  const cookies = await page.context().cookies();
  expect(
    cookies.find((cookie) => cookie.name === "yugitalk_session")?.httpOnly,
  ).toBe(true);
  await page.reload();
  await expect(page.locator("#prompt")).toBeVisible();
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(
    page.getByRole("button", { name: "登录", exact: true }),
  ).toBeVisible();
  await page.context().addCookies(cookies);
  expect((await page.request.get("/api/v1/auth/me")).status()).toBe(401);
});

for (const width of [360, 768, 1440]) {
  test(`${width}px 登录及工作台布局、两种主题、无障碍与输入焦点`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("/");
    await expect(
      page.getByRole("button", { name: "登录", exact: true }),
    ).toBeVisible();
    expect(
      (await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze())
        .violations,
    ).toEqual([]);
    await login(page);
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
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
      path: `../docs/stages/artifacts/stage2-light-${width}.png`,
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
  });
}

test("创建空间、添加成员、修改角色、移除及空间切换", async ({ page }) => {
  await login(page);
  await page.getByRole("button", { name: "设置", exact: true }).click();
  // 重复添加已有所有者不应被误报为服务故障，也不能改变原角色。
  await page.getByLabel("添加已初始化用户的邮箱").fill(owner);
  await page.getByRole("button", { name: "添加成员" }).click();
  await expect(page.getByRole("alert")).toContainText("已是当前工作空间成员");
  await expect(page.getByLabel(`${owner} 的角色`, { exact: true })).toHaveValue(
    "owner",
  );
  await page
    .getByLabel("添加已初始化用户的邮箱")
    .fill("uninitialized@example.test");
  await page.getByRole("button", { name: "添加成员" }).click();
  await expect(page.getByRole("alert")).toContainText("请先由管理员初始化账号");
  await page.getByLabel("新工作空间名称").fill("浏览器验收空间");
  await page.getByRole("button", { name: "创建空间" }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "操作已完成" }),
  ).toBeVisible();
  await page
    .getByLabel("当前工作空间")
    .selectOption({ label: "浏览器验收空间" });
  await page.getByRole("button", { name: "设置", exact: true }).click();
  await page.getByLabel("添加已初始化用户的邮箱").fill(member);
  await page.getByRole("button", { name: "添加成员" }).click();
  const role = page.getByLabel(`${member} 的角色`, { exact: true });
  await expect(role).toHaveValue("member");
  await role.selectOption("admin");
  await expect(role).toHaveValue("admin");
  await expect(
    page.getByRole("status").filter({ hasText: "操作已完成" }),
  ).toBeVisible();
  await page
    .getByRole("listitem")
    .filter({ hasText: member })
    .getByRole("button", { name: "移除" })
    .click();
  await expect(role).toHaveCount(0);
});

test("服务端 RBAC、跨空间隔离、CSRF 与审计权限", async ({ playwright }) => {
  const admin = await playwright.request.newContext({ baseURL: origin });
  const ordinary = await playwright.request.newContext({ baseURL: origin });
  try {
    const a = await authenticate(admin, owner);
    const b = await authenticate(ordinary, member);
    const space = a.workspaces.find(
      (item: { name: string }) => item.name === "所有者空间",
    ).id;
    expect((await ordinary.get(`/api/v1/workspaces/${space}`)).status()).toBe(
      404,
    );
    const headers = { Origin: origin, "X-CSRF-Token": a.csrf_token };
    expect(
      (
        await admin.post(`/api/v1/workspaces/${space}/members`, {
          data: { email: member, role: "member" },
          headers,
        })
      ).status(),
    ).toBe(201);
    expect((await ordinary.get(`/api/v1/workspaces/${space}`)).status()).toBe(
      200,
    );
    expect(
      (await ordinary.get(`/api/v1/workspaces/${space}/members`)).status(),
    ).toBe(403);
    expect((await ordinary.get("/api/v1/admin/audit")).status()).toBe(403);
    expect((await admin.get("/api/v1/admin/audit")).status()).toBe(200);
    expect(
      (
        await admin.post("/api/v1/workspaces", {
          data: { name: "非法空间" },
          headers: { Origin: origin },
        })
      ).status(),
    ).toBe(403);
    expect(
      (
        await admin.post("/api/v1/workspaces", {
          data: { name: "非法空间" },
          headers: { ...headers, Origin: "https://evil.test" },
        })
      ).status(),
    ).toBe(403);
    expect(
      (
        await admin.delete(`/api/v1/workspaces/${space}/members/${b.user.id}`, {
          headers,
        })
      ).status(),
    ).toBe(204);
    expect((await ordinary.get(`/api/v1/workspaces/${space}`)).status()).toBe(
      404,
    );
    expect(
      (await admin.get(`/api/v1/workspaces/${b.workspaces[0].id}`)).status(),
    ).toBe(404);
  } finally {
    await admin.dispose();
    await ordinary.dispose();
  }
});

test("服务端撤销会话后写操作返回登录页", async ({ page }) => {
  await login(page);
  await page.getByRole("button", { name: "设置", exact: true }).click();
  await expect(page.getByLabel("新工作空间名称")).toBeVisible();
  await expect(
    page.getByRole("listitem").filter({ hasText: owner }),
  ).toBeVisible();
  const me = await (await page.request.get("/api/v1/auth/me")).json();
  expect(
    (
      await page.request.post("/api/v1/auth/logout", {
        headers: { Origin: origin, "X-CSRF-Token": me.csrf_token },
      })
    ).status(),
  ).toBe(204);
  await page.getByLabel("新工作空间名称").fill("过期会话不应创建");
  await page.getByRole("button", { name: "创建空间" }).click();
  await expect(
    page.getByRole("button", { name: "登录", exact: true }),
  ).toBeVisible();
});

test("真实 PostgreSQL 并发降级保留最后一个空间所有者", async ({
  playwright,
}) => {
  const first = await playwright.request.newContext({ baseURL: origin });
  const second = await playwright.request.newContext({ baseURL: origin });
  try {
    const a = await authenticate(first, owner);
    const b = await authenticate(second, member);
    const space = a.workspaces.find(
      (item: { name: string }) => item.name === "所有者空间",
    ).id;
    const ha = { Origin: origin, "X-CSRF-Token": a.csrf_token };
    const hb = { Origin: origin, "X-CSRF-Token": b.csrf_token };
    expect(
      (
        await first.post(`/api/v1/workspaces/${space}/members`, {
          data: { email: member, role: "member" },
          headers: ha,
        })
      ).status(),
    ).toBe(201);
    expect(
      (
        await first.patch(`/api/v1/workspaces/${space}/members/${b.user.id}`, {
          data: { role: "owner" },
          headers: ha,
        })
      ).status(),
    ).toBe(200);
    const results = await Promise.all([
      first.patch(`/api/v1/workspaces/${space}/members/${a.user.id}`, {
        data: { role: "member" },
        headers: ha,
      }),
      second.patch(`/api/v1/workspaces/${space}/members/${b.user.id}`, {
        data: { role: "member" },
        headers: hb,
      }),
    ]);
    expect(results.map((result) => result.status()).sort()).toEqual([200, 409]);
  } finally {
    await first.dispose();
    await second.dispose();
  }
});

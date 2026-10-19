import { afterEach, expect, test, vi } from "vitest";
import { identityRequest } from "./api";

afterEach(() => vi.unstubAllGlobals());

test("网络中断显示中文排障提示，不泄露原始浏览器错误", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockRejectedValue(new TypeError("Failed to fetch")),
  );
  await expect(
    identityRequest("/accounts/space/members"),
  ).rejects.toMatchObject({
    status: 0,
    message: "连接已中断，请检查网络或 SSH 隧道，然后重试。",
  });
});

test("写请求携带 CSRF 与同源 Cookie，成功的空响应无需 JSON", async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", fetch);
  await expect(
    identityRequest("/auth/logout", "POST", undefined, "csrf-value"),
  ).resolves.toBeUndefined();
  expect(fetch).toHaveBeenCalledWith(
    "/api/v1/auth/logout",
    expect.objectContaining({
      credentials: "same-origin",
      headers: expect.objectContaining({ "X-CSRF-Token": "csrf-value" }),
    }),
  );
});

test("会话撤销触发重新登录通知", async () => {
  const dispatchEvent = vi.fn();
  vi.stubGlobal("window", { dispatchEvent });
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response("{}", { status: 401 })),
  );
  await expect(identityRequest("/accounts")).rejects.toMatchObject({
    status: 401,
  });
  expect(dispatchEvent.mock.calls[0][0].type).toBe("session-expired");
});

test("限流错误转换为可读提示", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response("{}", { status: 429 })),
  );
  await expect(identityRequest("/auth/login")).rejects.toMatchObject({
    status: 429,
  });
});

test.each([
  [
    429,
    "run_budget_insufficient",
    "剩余额度不足。请在 Agent 页降低单次任务预算并发布新版本，或联系管理员调整总额度。",
  ],
  [
    429,
    "run_concurrency_exceeded",
    "正在执行的任务已达上限，请等待完成或停止已有任务。",
  ],
  [
    409,
    "member_already_exists",
    "该用户已是当前工作空间成员，无需重复添加。修改权限请使用下方成员列表。",
  ],
  [
    404,
    "member_user_unavailable",
    "用户不存在或已停用，请先由管理员初始化账号，再添加成员。",
  ],
  [409, "last_account_owner", "不能移除或降级工作空间的最后一位所有者。"],
])("业务错误 %s %s 提供明确提示", async (status, code, message) => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ code }), { status })),
  );
  await expect(
    identityRequest("/accounts/space/members", "POST"),
  ).rejects.toMatchObject({ status, message });
});

test("非 JSON 与未知错误码保持安全提示", async () => {
  for (const body of [
    "<html>proxy error</html>",
    JSON.stringify({ code: "unknown", message: "private-data" }),
    JSON.stringify({ code: "member_already_exists", message: "private-data" }),
  ]) {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(body, { status: 500 })),
    );
    await expect(identityRequest("/accounts")).rejects.toMatchObject({
      status: 500,
      message: "请求失败，请检查服务状态。",
    });
  }
});

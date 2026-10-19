import { afterEach, expect, test, vi } from "vitest";
import { fetchHealth } from "./api";

afterEach(() => vi.unstubAllGlobals());
test("就绪检查 503 保留依赖故障状态", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({ status: "unavailable", checks: { redis: false } }),
          { status: 503 },
        ),
      ),
  );
  expect(await fetchHealth()).toEqual({
    status: "unavailable",
    checks: { redis: false },
  });
});
test("拒绝格式错误或非健康接口的错误响应", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({ status: "ready", checks: { redis: "yes" } }),
        ),
      ),
  );
  await expect(fetchHealth()).rejects.toThrow("响应格式错误");
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response("{}", { status: 500 })),
  );
  await expect(fetchHealth()).rejects.toThrow("请求失败");
});

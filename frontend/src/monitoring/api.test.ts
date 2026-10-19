import { afterEach, expect, test, vi } from "vitest";
import { fetchSnapshot } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

test("监控接口携带同源会话和取消信号，普通用户收到明确提示", async () => {
  vi.spyOn(console, "warn").mockImplementation(() => undefined);
  const fetch = vi.fn().mockResolvedValue(new Response("{}", { status: 403 }));
  vi.stubGlobal("fetch", fetch);
  const signal = new AbortController().signal;
  await expect(fetchSnapshot(signal)).rejects.toMatchObject({
    status: 403,
    message: "实时监控仅限管理员访问。",
  });
  expect(fetch).toHaveBeenCalledWith("/api/v1/admin/monitoring", {
    credentials: "same-origin",
    signal,
  });
});

test("网络异常提供中文恢复提示，排除原始错误内容", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  vi.stubGlobal(
    "fetch",
    vi.fn().mockRejectedValue(new TypeError("private-content")),
  );
  await expect(
    fetchSnapshot(new AbortController().signal),
  ).rejects.toMatchObject({
    status: 0,
    message: "监控连接中断，请检查网络后刷新。",
  });
});

test("非 JSON 响应不直接显示原始内容", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response("private-content")),
  );
  await expect(
    fetchSnapshot(new AbortController().signal),
  ).rejects.toMatchObject({ message: "监控响应格式错误，请稍后刷新。" });
});

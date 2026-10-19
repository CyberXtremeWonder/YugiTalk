import { afterEach, expect, test, vi } from "vitest";
import {
  diagnosticRoute,
  installRuntimeMonitoring,
  monitoredFetch,
  reportRuntimeFailure,
} from "./monitoring";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("路由诊断排除资源编号、查询内容及文件名", () => {
  expect(
    diagnosticRoute(
      "/api/v1/knowledge-bases/private-id/documents?filename=secret.txt",
    ),
  ).toBe("/api/v1/knowledge-bases/:id/documents");
});

test("服务错误记录状态和请求关联，不读取正文或凭证", async () => {
  const log = vi.spyOn(console, "error").mockImplementation(() => undefined);
  const response = new Response("private-response", {
    status: 500,
    headers: { "X-Request-ID": "request-123", "X-Trace-ID": "trace-123" },
  });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response));
  expect(
    await monitoredFetch("/api/v1/agents/private-id?secret=private-query", {
      headers: { Authorization: "private-token" },
      body: "private-body",
      method: "POST",
    }),
  ).toBe(response);
  expect(log).toHaveBeenCalledWith(
    "[YugiTalk]",
    expect.objectContaining({
      route: "/api/v1/agents/:id",
      status_code: 500,
      request_id: "request-123",
      trace_id: "trace-123",
    }),
  );
  expect(JSON.stringify(log.mock.calls)).not.toContain("private");
  expect(await response.text()).toBe("private-response");
});

test("网络失败保留异常供页面处理，主动取消不报错", async () => {
  const log = vi.spyOn(console, "error").mockImplementation(() => undefined);
  const failure = new TypeError("private-error");
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(failure));
  await expect(monitoredFetch("/health/ready")).rejects.toBe(failure);
  expect(log).toHaveBeenCalledTimes(1);
  log.mockClear();
  const controller = new AbortController();
  controller.abort();
  await expect(
    monitoredFetch("/health/ready", { signal: controller.signal }),
  ).rejects.toBe(failure);
  expect(log).not.toHaveBeenCalled();
});

test("全局异常监听和运行错误均排除原始异常信息", () => {
  const log = vi.spyOn(console, "error").mockImplementation(() => undefined);
  const listeners = new Map<
    string,
    (event: { error?: unknown; reason?: unknown }) => void
  >();
  vi.stubGlobal("window", {
    addEventListener: (
      type: string,
      listener: (event: { error?: unknown; reason?: unknown }) => void,
    ) => listeners.set(type, listener),
  });
  installRuntimeMonitoring();
  listeners.get("error")!({ error: new TypeError("private-message") });
  listeners.get("unhandledrejection")!({ reason: "private-rejection" });
  reportRuntimeFailure("events.read", new Error("private-stream"));
  expect(log).toHaveBeenCalledTimes(3);
  expect(JSON.stringify(log.mock.calls)).not.toContain("private");
});

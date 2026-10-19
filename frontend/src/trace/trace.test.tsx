import { afterEach, expect, test, vi } from "vitest";
import { tokenLabel, timing } from "./TraceView";
import { fetchTrace, fetchTraces, type TraceStep } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

test("真实用量缺失显示未知，零用量仍显示零", () => {
  expect(tokenLabel(null)).toBe("未知");
  expect(tokenLabel(0)).toBe("0");
  expect(tokenLabel(12000)).toBe("12,000");
});

test("耗时条反映相对起点和持续时间", () => {
  const start = Date.parse("2026-10-14T00:00:00Z");
  const step = {
    started_at: "2026-10-14T00:00:01Z",
    elapsed_ms: 2000,
  } as TraceStep;
  expect(timing(step, start, 10000)).toEqual({ left: "10%", width: "20%" });
});

test("Trace 请求使用管理员接口和同源会话", async () => {
  const fetch = vi.fn().mockImplementation(async () => new Response("{}"));
  vi.stubGlobal("fetch", fetch);
  await fetchTraces("abc");
  await fetchTrace("run-id");
  expect(fetch.mock.calls[0][0]).toBe("/api/v1/admin/traces?cursor=abc");
  expect(fetch.mock.calls[1][0]).toBe("/api/v1/admin/traces/run-id");
  expect(fetch.mock.calls[1][1].credentials).toBe("same-origin");
});

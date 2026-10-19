import { afterEach, expect, test, vi } from "vitest";
import { readEvents, type Run, type RunEvent } from "./api";
afterEach(() => vi.unstubAllGlobals());
const run = { id: "run", events_url: "/events" } as Run;
/** 构造带序号的公开事件帧，供断线续传测试复用。 */
function frame(sequence: number, type = "text_delta") {
  return `id: run:${sequence}\nevent: ${type}\ndata: ${JSON.stringify({ sequence, type, payload: { text: "你好" } })}\n\n`;
}
test("跨字节 UTF-8 分片、重复事件与终态只消费一次", async () => {
  const bytes = new TextEncoder().encode(
    frame(1) + frame(1) + frame(2, "completed"),
  );
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(
        new ReadableStream({
          /** 消费受控事件流并收集回调结果，供去重与恢复测试使用。 */
          start(controller) {
            for (let index = 0; index < bytes.length; index++)
              controller.enqueue(bytes.slice(index, index + 1));
            controller.close();
          },
        }),
      ),
    ),
  );
  const events: RunEvent[] = [];
  await readEvents(run, new AbortController().signal, (event) =>
    events.push(event),
  );
  expect(events.map((event) => event.sequence)).toEqual([1, 2]);
  expect(events[0].payload.text).toBe("你好");
});
test("断线以最后消费的事件游标续传", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(new Response(frame(1)))
    .mockResolvedValueOnce(new Response(frame(1) + frame(2, "completed")));
  vi.stubGlobal("fetch", fetch);
  const events: RunEvent[] = [];
  await readEvents(run, new AbortController().signal, (event) =>
    events.push(event),
  );
  expect(fetch.mock.calls[1][1].headers).toEqual({ "Last-Event-ID": "run:1" });
  expect(events.map((event) => event.sequence)).toEqual([1, 2]);
});

test("恢复任务重读旧终态后，继续消费新的检查点与终态", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(
        new Response(
          frame(1, "error") +
            frame(2, "checkpoint_restored") +
            frame(3, "completed"),
        ),
      ),
  );
  const events: RunEvent[] = [];
  await readEvents(
    { ...run, event_sequence: 2 },
    new AbortController().signal,
    (event) => events.push(event),
  );
  expect(events.map((event) => event.sequence)).toEqual([1, 2, 3]);
});

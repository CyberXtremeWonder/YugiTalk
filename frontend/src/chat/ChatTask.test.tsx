import { expect, test, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { Run } from "../tasks/api";
import { ChatTask } from "./ChatTask";

/** 不启动事件订阅，直接验证各任务状态对应的操作与提示。 */
function renderTask(status: string, controlling = false) {
  return renderToStaticMarkup(
    <ChatTask
      task={{ id: "task", status, error_code: null } as Run}
      onControl={vi.fn()}
      controlling={controlling}
      onTask={vi.fn()}
      onHistory={vi.fn()}
      onClarification={vi.fn()}
      replay={false}
    />,
  );
}

test("暂停的任务提供继续与停止，并停止显示正在思考", () => {
  const html = renderTask("paused");
  expect(html).toContain("继续思考</button>");
  expect(html).toContain("停止思考</button>");
  expect(html).toContain("Agent 已暂停思考");
  expect(html).not.toContain("Agent 正在思考中");
  expect(html).not.toContain("暂停思考</button>");
});

test("排队和运行任务可以暂停或停止，已停止任务不再显示运行操作", () => {
  for (const status of ["queued", "running"]) {
    const html = renderTask(status);
    expect(html).toContain("暂停思考</button>");
    expect(html).toContain("停止思考</button>");
  }
  const html = renderTask("cancelled");
  expect(html).not.toContain("任务控制");
  expect(html).not.toContain("Agent 正在思考中");
});

test("控制请求期间禁止重复点击暂停或停止", () => {
  const html = renderTask("paused", true);
  expect(html.match(/disabled=""/g)).toHaveLength(2);
});

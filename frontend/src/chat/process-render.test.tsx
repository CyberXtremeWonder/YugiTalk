import { expect, test } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ProcessSummary } from "./ProcessSummary";

test("多轮检索中的相同阶段只展示一次，按首次出现顺序保留", () => {
  const html = renderToStaticMarkup(
    <ProcessSummary
      busy={false}
      steps={[
        "并行检索资料",
        "核对证据是否充分",
        "并行检索资料",
        "核对证据是否充分",
        "处理完成",
      ]}
    />,
  );
  expect(html.match(/<li>/g)).toHaveLength(3);
  expect(html.match(/并行检索资料/g)).toHaveLength(1);
  expect(html.match(/核对证据是否充分/g)).toHaveLength(1);
  expect(html.indexOf("并行检索资料")).toBeLessThan(html.indexOf("处理完成"));
});

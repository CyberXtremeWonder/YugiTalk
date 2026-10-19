import { expect, test } from "vitest";
import { withoutSourceLabels } from "./presentation";

test("正文隐藏相邻索引并保留段落、链接文本与代码格式", () => {
  expect(
    withoutSourceLabels("答案[S2][S3]。\n\n**结论** [ S12 ]\n`code`"),
  ).toBe("答案。\n\n**结论** \n`code`");
});

test("跨增量索引不闪烁且保留后续正文", () => {
  for (const text of ["答案[", "答案[S", "答案[S2", "答案[S23", "答案[S23]"])
    expect(withoutSourceLabels(text, true)).toBe("答案");
  expect(withoutSourceLabels("答案[S23]继续", true)).toBe("答案继续");
});

test("普通 Markdown 内容与完成后的非索引方括号保持原样", () => {
  expect(withoutSourceLabels("[说明](url)\n[Something] [S] [T2] [")).toBe(
    "[说明](url)\n[Something] [S] [T2] [",
  );
});

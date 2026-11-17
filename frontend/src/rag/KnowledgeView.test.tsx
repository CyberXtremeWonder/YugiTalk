import { renderToStaticMarkup } from "react-dom/server";
import { expect, test } from "vitest";
import { KnowledgeView } from "./KnowledgeView";

test("上传界面默认自动识别，并提供五种手动分块方案", () => {
  const html = renderToStaticMarkup(
    <KnowledgeView
      identity={{
        csrf_token: "test",
        user: { id: "user", email: "a@test", name: "A", platform_admin: false },
      }}
      account={{ id: "user", name: "A" }}
      base=""
      setBase={() => {}}
    />,
  );
  expect(html).toContain("文档分块类型");
  expect(html).toContain('value="auto" selected=""');
  for (const profile of ["short", "long", "tokens", "structured", "ultra"])
    expect(html).toContain(`value="${profile}"`);
});

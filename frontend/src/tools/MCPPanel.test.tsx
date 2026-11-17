import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { MCPPanel } from "./MCPPanel";
import type { Identity } from "../identity/api";

describe("MCP server configuration", () => {
  it("offers both remote transports and a write-only credential field", () => {
    const html = renderToStaticMarkup(
      <MCPPanel identity={{ csrf_token: "test" } as Identity} />,
    );
    expect(html).toContain("Streamable HTTP");
    expect(html).toContain("SSE");
    expect(html).toContain('type="password"');
    expect(html).toContain("保存配置");
    expect(html).not.toContain("Stdio");
  });
});

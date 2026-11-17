import { expect, test } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { Attachments } from "./Attachments";

test("聊天仅对已授权的后端文件路径展示下载链接", () => {
  const html = renderToStaticMarkup(
    <Attachments
      files={[
        {
          filename: "报告.pdf",
          size: 1024,
          available: true,
          download_url: "/api/v1/research/1234-abcd/files/pdf",
        },
        {
          filename: "失效.docx",
          size: 1024,
          available: false,
          download_url: null,
        },
        {
          filename: "恶意.md",
          size: 1,
          available: true,
          download_url: "https://evil.example",
        },
      ]}
    />,
  );
  expect(html).toContain('href="/api/v1/research/1234-abcd/files/pdf"');
  expect(html).not.toContain("evil.example");
  expect(html.match(/<a /g)).toHaveLength(1);
  expect(html).toContain("来源已失效");
  expect(html).toContain("下载链接无效，请刷新页面");
});

test("ZIP 和抓取图片以受保护的聊天附件下载", () => {
  const html = renderToStaticMarkup(
    <Attachments
      files={[
        {
          filename: "资料.zip",
          size: 100,
          available: true,
          download_url: "/api/v1/research/abcd-1234/files/zip",
        },
        {
          filename: "image-1.png",
          size: 100,
          available: true,
          download_url: "/api/v1/research/abcd-1234/files/picture-0",
        },
        {
          filename: "无效.png",
          size: 100,
          available: true,
          download_url: "/api/v1/research/abcd-1234/files/picture-6",
        },
      ]}
    />,
  );
  expect(html).toContain('href="/api/v1/research/abcd-1234/files/zip"');
  expect(html).toContain('href="/api/v1/research/abcd-1234/files/picture-0"');
  expect(html.match(/<a /g)).toHaveLength(2);
  expect(html).not.toContain("来源已失效");
});

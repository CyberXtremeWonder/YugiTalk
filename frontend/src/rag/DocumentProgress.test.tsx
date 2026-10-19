import { expect, test } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { DocumentIndexProgress, DocumentProgress } from "./DocumentProgress";
import type { Document } from "./api";

const doc: Document = {
  id: "doc",
  filename: "说明.md",
  size: 123,
  status: "indexing",
  error_code: null,
  index_version: 1,
  progress: {
    stage: "embedding",
    total_chunks: 42,
    embedded_chunks: 16,
    indexed_chunks: 16,
    batch_number: 2,
  },
};
test("文件列表进度按已写入数量显示，编码完成不等于索引完成", () => {
  const html = renderToStaticMarkup(
    <DocumentIndexProgress
      doc={{ ...doc, progress: { ...doc.progress, embedded_chunks: 42 } }}
    />,
  );
  expect(html).toContain('max="42" value="16"');
  expect(html).toContain("38% · 已索引 16/42 块");
  const queued = renderToStaticMarkup(
    <DocumentIndexProgress doc={{ ...doc, status: "queued", progress: {} }} />,
  );
  expect(queued).toContain("尚未确定分块总数");
  expect(queued).not.toContain("value=");
  expect(
    renderToStaticMarkup(
      <DocumentIndexProgress doc={{ ...doc, status: "ready" }} />,
    ),
  ).toBe("");
});
test("导入进度显示真实分块数量，未完成的阶段不标为完成", () => {
  const html = renderToStaticMarkup(<DocumentProgress doc={doc} />);
  expect(html).toContain("Embedding：16/42 块");
  expect(html).toContain("Milvus：16/42 块");
  expect(html).toContain('max="42" value="16"');
  expect(html).toContain('data-state="active"');
  expect(html).toContain('data-state="pending"');
});
test("失败显示具体步骤、安全原因和保留数量", () => {
  const html = renderToStaticMarkup(
    <DocumentProgress
      doc={{
        ...doc,
        status: "failed",
        error_code: "embedding_timeout",
        progress: { ...doc.progress, failed_stage: "embedding" },
      }}
    />,
  );
  expect(html).toContain("失败步骤：Embedding 编码");
  expect(html).toContain("请求超时");
  expect(html).toContain('data-state="failed"');
  expect(html).toContain('data-state="paused"');
  expect(html).toContain("16/42 块");
});
test("重试归零，旧文档不伪造失败位置", () => {
  const pending = renderToStaticMarkup(
    <DocumentProgress doc={{ ...doc, status: "queued", progress: {} }} />,
  );
  expect(pending).not.toContain("16/42");
  expect(pending).not.toContain("失败步骤");
  const legacy = renderToStaticMarkup(
    <DocumentProgress
      doc={{
        ...doc,
        status: "failed",
        progress: {},
        error_code: "document_index_failed",
      }}
    />,
  );
  expect(legacy).toContain("历史任务未记录具体步骤");
});

test("成功索引后清空流程展示", () => {
  expect(
    renderToStaticMarkup(
      <DocumentProgress doc={{ ...doc, status: "ready" }} />,
    ),
  ).toBe("");
});

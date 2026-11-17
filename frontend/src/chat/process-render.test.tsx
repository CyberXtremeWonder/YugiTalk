import { expect, test } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { appendProcessEvent, ProcessSummary } from "./ProcessSummary";

test("多轮 RAG 只渲染一次检索和结束，隐藏内部阶段", () => {
  let steps: string[] = [];
  for (const stage of [
    "tool",
    "rag_plan",
    "rag_retrieve",
    "rag_plan",
    "rag_retrieve",
    "rag_rerank",
    "rag_grade",
    "rag_rerank",
    "rag_grade",
    "rag_synthesis",
    "tool",
  ]) {
    steps = appendProcessEvent(steps, {
      sequence: 1,
      type: "tool_status",
      payload: { stage, name: "knowledge_search" },
    });
  }
  for (let i = 0; i < 2; i++) {
    steps = appendProcessEvent(steps, {
      sequence: 1,
      type: "tool_status",
      payload: { stage: "tool", name: "knowledge_search", status: "succeeded" },
    });
  }
  const html = renderToStaticMarkup(
    <ProcessSummary busy={false} steps={[...steps, "处理完成"]} />,
  );
  expect(html.match(/<li>/g)).toHaveLength(3);
  expect(html.match(/检索知识库/g)).toHaveLength(1);
  expect(html.match(/知识库检索结束/g)).toHaveLength(1);
  for (const text of [
    "拆分检索",
    "并行检索",
    "融合",
    "核对证据",
    "整理检索证据",
    "知识库检索完成",
  ]) {
    expect(html).not.toContain(text);
  }
  expect(html.indexOf("检索知识库")).toBeLessThan(html.indexOf("处理完成"));
  expect(html.indexOf("检索知识库")).toBeLessThan(html.indexOf("知识库检索结束"));
});

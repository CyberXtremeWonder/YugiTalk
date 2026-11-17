import { describe, expect, it } from "vitest";
import { appendProcessEvent, processStep } from "./ProcessSummary";

describe("Agent 闭环进度", () => {
  it("分别展示插话已接收和已加入任务", () => {
    expect(processStep({ sequence: 1, type: "tool_status", payload: {
      stage: "steering_queued",
    } })).toBe("已收到插话，等待后续处理");
    expect(processStep({ sequence: 2, type: "tool_status", payload: {
      stage: "steering_applied",
    } })).toBe("已将插话加入当前任务");
  });
  it("区分直接生成文件与补充研究，不显示内部工具名", () => {
    expect(processStep({ sequence: 1, type: "tool_status", payload: {
      stage: "tool", name: "export_document", status: "running",
    } })).toBe("开始调用工具：生成文件");
    expect(processStep({ sequence: 2, type: "tool_status", payload: {
      stage: "tool", name: "research_report", status: "running",
    } })).toBe("开始调用工具：研究并生成文件");
  });
  it("多次工具调用和模型处理只显示一次同名提示，保留不同工具与失败状态", () => {
    let entries: string[] = [];
    const emit = (stage: string, name?: string, status?: string) => {
      entries = appendProcessEvent(entries, {
        sequence: 1,
        type: "tool_status",
        payload: { stage, name, status },
      });
    };
    for (let i = 0; i < 3; i++) {
      emit("model");
      emit("tool", "web_search", "running");
      emit("tool", "web_search", "succeeded");
    }
    emit("tool", "web_search", "failed");
    emit("tool", "weather_query", "running");
    expect(entries).toEqual([
      "模型处理请求",
      "开始调用工具：网页搜索",
      "工具已完成：网页搜索",
      "调用工具：网页搜索（查询失败）",
      "开始调用工具：天气查询",
    ]);
  });

  it("知识库检索只展示开始和成功结束，不展示内部 RAG 阶段", () => {
    let entries: string[] = [];
    const stages = [
      "rag_plan",
      "rag_retrieve",
      "rag_rerank",
      "rag_grade",
      "rag_rewrite",
      "rag_synthesis",
      "rag_resume",
      "retrieval",
      "tool",
    ];
    for (const id of ["A", "B", ""]) {
      for (const stage of stages) {
        for (const status of [
          "running",
          "succeeded",
          "fallback",
          "circuit_open",
          "failed",
        ]) {
          const event = {
            sequence: 1,
            type: "tool_status",
            payload: {
              stage,
              status,
              name: "knowledge_search",
              retrieval_id: id,
              query_count: 3,
            },
          };
          expect(processStep(event)).toBe(
            stage === "tool" && status === "succeeded"
              ? "知识库检索结束"
              : "检索知识库",
          );
          entries = appendProcessEvent(entries, event);
        }
      }
    }
    expect(entries).toEqual(["检索知识库", "知识库检索结束"]);
  });

  it("展示计划的行动与完成标准", () => {
    expect(
      processStep({
        sequence: 1,
        type: "tool_status",
        payload: {
          stage: "agent_plan",
          status: "ready",
          steps: [{ title: "查询天气", criterion: "获得指定城市当天数据" }],
        },
      }),
    ).toContain("查询天气（完成标准：获得指定城市当天数据）");
  });

  it("展示检查结果与修订阶段", () => {
    expect(
      processStep({
        sequence: 2,
        type: "tool_status",
        payload: {
          stage: "agent_reflect",
          status: "incomplete",
          steps: [
            { title: "查询天气", criterion: "有数据", status: "incomplete" },
          ],
        },
      }),
    ).toBe("检查发现缺口：查询天气（未完成）");
    expect(
      processStep({
        sequence: 3,
        type: "tool_status",
        payload: {
          stage: "agent_revise",
        },
      }),
    ).toBe("根据检查缺口修订计划");
  });
});

import { describe, expect, it } from "vitest";
import { processStep } from "./ProcessSummary";

describe("Agent 闭环进度", () => {
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

import type { RunEvent } from "../tasks/api";

// 只展示服务端公开的流程与工具事件，不拼接模型内部推理文本。
/** 将公开模型和工具事件映射成简短的流程条目。 */
export function processStep(event: RunEvent): string | null {
  if (event.type === "checkpoint_restored") return "恢复已保存的流程";
  if (event.type === "clarification") return "等待补充信息";
  if (event.type === "completed") return "处理完成";
  if (event.type === "error")
    return event.payload.error_code === "cancelled" ? "已停止" : "任务未完成";
  if (event.type !== "tool_status") return null;
  if (event.payload.stage === "skill")
    return `应用 Skill：${event.payload.skill_name || "自定义流程"}`;
  if (event.payload.stage === "agent_plan")
    return event.payload.steps
      ? `行动计划：${event.payload.steps.map((step, index) => `${index + 1}. ${step.title}（完成标准：${step.criterion}）`).join("；")}`
      : "正在制定行动计划";
  if (event.payload.stage === "agent_revise") return "根据检查缺口修订计划";
  if (event.payload.stage === "agent_reflect")
    return event.payload.steps
      ? `检查${event.payload.status === "passed" ? "通过" : "发现缺口"}：${event.payload.steps.map((step) => `${step.title}（${step.status === "completed" ? "已完成" : "未完成"}）`).join("；")}`
      : "正在核对完成标准与执行证据";
  const ragStages: Record<string, string> = {
    rag_plan: "判断问题与拆分检索",
    rag_retrieve: "并行检索资料",
    rag_rerank:
      event.payload.status === "fallback" ||
      event.payload.status === "circuit_open"
        ? "沿用融合结果"
        : "融合与可选重排",
    rag_grade: "核对证据是否充分",
    rag_rewrite: "调整查询后再检索一次",
    rag_synthesis: "合成答案与来源",
    rag_resume: "根据补充信息恢复",
  };
  if (event.payload.stage && ragStages[event.payload.stage])
    return ragStages[event.payload.stage];
  if (event.payload.stage === "retrieval") return "检索授权知识库";
  if (event.payload.stage === "running") return "理解任务";
  if (event.payload.stage === "model") return "分析任务与生成答复";
  if (event.payload.stage === "recovery") return "从检查点恢复任务";
  if (event.payload.stage === "tool") {
    const names: Record<string, string> = {
      web_search: "网页搜索",
      text_stats: "文本统计",
      format_report: "报告排版",
      weather_query: "天气查询",
      traffic_query: "路况查询",
    };
    const name =
      names[event.payload.name || ""] || event.payload.name || "授权工具";
    return `调用工具：${name}${event.payload.status === "failed" ? (event.payload.error_code === "tool_arguments_invalid" ? "（需要修正参数）" : event.payload.error_code === "tool_result_too_large" ? "（返回内容过大）" : "（查询失败）") : ""}`;
  }
  return null;
}

/** 展示可折叠的处理流程及工具调用，不显示内部推理内容。 */
export function ProcessSummary({
  steps,
  busy,
}: {
  steps: string[];
  busy: boolean;
}) {
  if (!busy && !steps.length) return null;
  return (
    <details className="process-summary" open>
      <summary>
        <svg aria-hidden="true" viewBox="0 0 24 24">
          <g fill="none" stroke="currentColor" strokeWidth="1.4">
            <ellipse cx="12" cy="12" rx="9" ry="4" />
            <ellipse
              cx="12"
              cy="12"
              rx="9"
              ry="4"
              transform="rotate(60 12 12)"
            />
            <ellipse
              cx="12"
              cy="12"
              rx="9"
              ry="4"
              transform="rotate(120 12 12)"
            />
          </g>
          <circle cx="12" cy="12" r="1.8" fill="currentColor" />
        </svg>
        <span>{busy ? "正在处理" : "处理过程"}</span>
        <span className="process-chevron" aria-hidden="true">
          ⌄
        </span>
      </summary>
      <ol>
        {(steps.length ? steps : ["准备任务"]).map((step, index) => (
          <li key={`${index}:${step}`}>{step}</li>
        ))}
      </ol>
    </details>
  );
}

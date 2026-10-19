import { useEffect, useState } from "react";
import {
  fetchTrace,
  fetchTraces,
  type TraceDetail,
  type TraceRun,
  type TraceStep,
} from "./api";
import "./trace.css";

const statuses: Record<string, string> = {
  queued: "排队中",
  running: "执行中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  waiting_input: "等待补充",
  waiting_approval: "等待确认",
  interrupted: "已中断",
};
const names: Record<string, string> = {
  rag_plan: "查询规划",
  rag_retrieve: "知识库检索",
  rag_rerank: "结果重排",
  rag_grade: "证据评判",
  rag_rewrite: "查询改写",
  rag_synthesis: "证据合成",
};

/** 缺失的供应商用量不转换为零。 */
export function tokenLabel(value: number | null): string {
  return value === null ? "未知" : value.toLocaleString("zh-CN");
}

/** 步骤条使用实际起止时间，父步骤和子步骤不会累计为总耗时。 */
export function timing(step: TraceStep, start: number, total: number) {
  const left = Math.max(
    0,
    Math.min(100, ((Date.parse(step.started_at) - start) / total) * 100),
  );
  const width = Math.max(
    0.5,
    Math.min(100 - left, ((step.elapsed_ms || 0) / total) * 100),
  );
  return { left: `${left}%`, width: `${width}%` };
}

/** 递归呈现执行批次、循环和请求，保留原生折叠及键盘操作。 */
function StepRow({
  step,
  steps,
  start,
  total,
}: {
  step: TraceStep;
  steps: TraceStep[];
  start: number;
  total: number;
}) {
  const children = steps.filter((item) => item.parent_id === step.id);
  const label =
    step.kind === "loop"
      ? `LOOP ${step.iteration}`
      : step.kind === "execution"
        ? `执行批次 ${step.attempt}`
        : names[step.name] || step.name;
  const info =
    step.kind === "llm"
      ? `${tokenLabel(step.input_tokens)} in / ${tokenLabel(step.output_tokens)} out`
      : step.kind === "memory"
        ? `${step.details.memory_reads || 0} 次读取 · ${step.details.memory_hits || 0} 条返回`
        : step.kind === "retrieval"
          ? Object.entries(step.details)
              .filter(([key]) => key !== "stage")
              .map(([key, value]) => `${key}: ${value}`)
              .join(" · ")
          : step.details.reused
            ? "复用已完成结果"
            : "";
  const content = (
    <>
      <span className="trace-kind">{step.kind.toUpperCase()}</span>
      <span className="trace-step-name">
        {label}
        <small>{info}</small>
      </span>
      <span className={`trace-status ${step.status}`}>
        {statuses[step.status] || step.status}
      </span>
      <span className="trace-duration">
        {step.elapsed_ms === null
          ? "未知耗时"
          : `${(step.elapsed_ms / 1000).toFixed(2)}s`}
      </span>
      <span className="trace-bar" aria-hidden="true">
        <i style={timing(step, start, total)} />
      </span>
    </>
  );
  return children.length ? (
    <details className="trace-group" open>
      <summary className="trace-row">{content}</summary>
      <div className="trace-children">
        {children.map((child) => (
          <StepRow
            key={child.id}
            step={child}
            steps={steps}
            start={start}
            total={total}
          />
        ))}
      </div>
    </details>
  ) : (
    <div className="trace-row trace-leaf">
      {content}
      {step.details.error_code && (
        <small className="trace-error">{step.details.error_code}</small>
      )}
    </div>
  );
}

/** 列表与步骤明细每三秒刷新，切换任务时忽略旧请求。 */
export function TraceView() {
  const [runs, setRuns] = useState<TraceRun[]>([]);
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState<TraceDetail | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [paused, setPaused] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  useEffect(() => {
    let active = true;
    let timeout: ReturnType<typeof setTimeout>;
    /** 请求结束后安排刷新，避免慢请求重叠。 */
    async function update() {
      try {
        const page = await fetchTraces();
        if (active) {
          setRuns((previous) =>
            paused
              ? [
                  ...page.items,
                  ...previous.filter(
                    (item) => !page.items.some((run) => run.id === item.id),
                  ),
                ]
              : page.items,
          );
          if (!paused) setCursor(page.next_cursor);
          setSelected((previous) => previous || page.items[0]?.id || "");
          setError("");
        }
      } catch (failure) {
        if (active)
          setError(
            failure instanceof Error ? failure.message : "Trace 加载失败",
          );
      } finally {
        if (active && !paused) timeout = setTimeout(update, 3000);
      }
    }
    void update();
    return () => {
      active = false;
      clearTimeout(timeout);
    };
  }, [paused, refresh]);
  useEffect(() => {
    if (!selected) return;
    let active = true;
    let timeout: ReturnType<typeof setTimeout>;
    setDetail(null);
    /** 详情独立刷新，完成任务停止轮询。 */
    async function update() {
      try {
        const data = await fetchTrace(selected);
        if (active) {
          setDetail(data);
          setError("");
          if (
            !paused &&
            ["running", "queued", "waiting_input", "waiting_approval"].includes(
              data.run.status,
            )
          )
            timeout = setTimeout(update, 3000);
        }
      } catch (failure) {
        if (active)
          setError(
            failure instanceof Error ? failure.message : "Trace 加载失败",
          );
      }
    }
    void update();
    return () => {
      active = false;
      clearTimeout(timeout);
    };
  }, [selected, paused, refresh]);
  const start = detail?.steps.length
    ? Math.min(...detail.steps.map((step) => Date.parse(step.started_at)))
    : 0;
  const end = detail?.steps.length
    ? Math.max(
        ...detail.steps.map(
          (step) => Date.parse(step.started_at) + (step.elapsed_ms || 0),
        ),
      )
    : start;
  const total = Math.max(1, end - start);
  return (
    <section
      className="secondary-view trace-view"
      aria-labelledby="trace-title"
    >
      <header className="trace-header">
        <div>
          <h1 id="trace-title">Trace</h1>
          <p>逐次查看任务执行、工具与记忆读取、真实模型用量。</p>
        </div>
        <div className="trace-controls">
          <button onClick={() => setPaused(!paused)}>
            {paused ? "恢复实时更新" : "暂停实时更新"}
          </button>
          <button onClick={() => setRefresh((value) => value + 1)}>刷新</button>
        </div>
      </header>
      {error && <p role="alert">{error}</p>}
      <div className="trace-layout">
        <aside className="trace-list" aria-label="任务列表">
          {!runs.length && <p>暂无任务记录</p>}
          {runs.map((run) => (
            <button
              key={run.id}
              aria-current={selected === run.id ? "true" : undefined}
              onClick={() => setSelected(run.id)}
            >
              <strong>{run.model}</strong>
              <span className={`trace-status ${run.status}`}>
                {statuses[run.status] || run.status}
              </span>
              <small>{new Date(run.created_at).toLocaleString("zh-CN")}</small>
              <code>{run.id.slice(0, 8)}</code>
            </button>
          ))}
          {cursor && (
            <button
              disabled={loadingMore}
              onClick={() => {
                setPaused(true);
                setLoadingMore(true);
                void fetchTraces(cursor)
                  .then((page) => {
                    setRuns((previous) => [
                      ...previous,
                      ...page.items.filter(
                        (run) => !previous.some((item) => item.id === run.id),
                      ),
                    ]);
                    setCursor(page.next_cursor);
                  })
                  .catch((failure) => setError(failure.message))
                  .finally(() => setLoadingMore(false));
              }}
            >
              {loadingMore ? "加载中…" : "加载更早任务"}
            </button>
          )}
        </aside>
        <div className="trace-detail" aria-label="Trace 详情">
          {!detail ? (
            <p role="status">
              {selected ? "正在加载执行记录…" : "选择任务查看详情"}
            </p>
          ) : (
            <>
              <div className="trace-run-heading">
                <code>{detail.run.id}</code>
                <span className={`trace-status ${detail.run.status}`}>
                  {statuses[detail.run.status] || detail.run.status}
                </span>
              </div>
              {detail.run.error_code && (
                <p className="trace-error">任务错误：{detail.run.error_code}</p>
              )}
              <p className="trace-model">
                {detail.run.provider} / {detail.run.model}
              </p>
              {detail.summary.recorded ? (
                <>
                  <div className="trace-metrics">
                    <span>
                      <strong>{(total / 1000).toFixed(1)}s</strong>执行跨度
                    </span>
                    <span>
                      <strong>{detail.summary.loops}</strong>模型循环
                    </span>
                    <span>
                      <strong>{detail.summary.model_calls}</strong>模型请求
                    </span>
                    <span>
                      <strong>{detail.summary.tool_calls}</strong>工具调用
                    </span>
                    <span>
                      <strong>
                        {detail.summary.memory_reads} /{" "}
                        {detail.summary.memory_hits}
                      </strong>
                      记忆读取 / 返回条数
                    </span>
                    <span>
                      <strong>{detail.summary.retrievals}</strong>知识库检索
                    </span>
                  </div>
                  <div className="trace-usage">
                    <strong>
                      {detail.summary.unknown_usage_calls ||
                      detail.summary.unrecorded_attempts
                        ? "已知部分："
                        : "真实用量："}
                      {tokenLabel(detail.summary.input_tokens)} in /{" "}
                      {tokenLabel(detail.summary.output_tokens)} out
                    </strong>
                    {detail.summary.unknown_usage_calls > 0 && (
                      <span>
                        {detail.summary.unknown_usage_calls} 次模型请求用量未知
                      </span>
                    )}
                    {detail.summary.unrecorded_attempts > 0 && (
                      <span>
                        {detail.summary.unrecorded_attempts}{" "}
                        个历史执行批次未记录，当前为部分 Trace
                      </span>
                    )}
                    {detail.summary.tool_reuses > 0 && (
                      <span>{detail.summary.tool_reuses} 次工具结果复用</span>
                    )}
                    <span>
                      保守额度 {tokenLabel(detail.run.charged_tokens)} ·
                      费用未配置
                    </span>
                  </div>
                  <div className="trace-waterfall">
                    <div className="trace-row trace-table-heading">
                      <span>类型</span>
                      <span>步骤 / 用量</span>
                      <span>状态</span>
                      <span>耗时</span>
                      <span>时间线</span>
                    </div>
                    {detail.steps
                      .filter((step) => !step.parent_id)
                      .map((step) => (
                        <StepRow
                          key={step.id}
                          step={step}
                          steps={detail.steps}
                          start={start}
                          total={total}
                        />
                      ))}
                  </div>
                </>
              ) : (
                <p>
                  此任务没有完整 Trace。历史任务不会补造步骤记录。已有输入 /
                  输出用量：{tokenLabel(detail.run.input_tokens)} /{" "}
                  {tokenLabel(detail.run.output_tokens)}。
                </p>
              )}
            </>
          )}
        </div>
      </div>
    </section>
  );
}

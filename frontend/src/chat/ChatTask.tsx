import { useEffect, useRef, useState } from "react";
import { identityRequest } from "../identity/api";
import { readEvents, type Run } from "../tasks/api";
import { runError } from "../tasks/errors";
import { Sources } from "../rag/Sources";
import { withoutSourceLabels } from "../rag/presentation";
import { Attachments } from "./Attachments";
import { ProcessSummary, appendProcessEvent } from "./ProcessSummary";
import { ResearchProgress } from "./ResearchProgress";
import { SafeMarkdown } from "./SafeMarkdown";
import type { Message } from "./api";

export const activeTask = (task: Run) =>
  ["queued", "running", "paused"].includes(task.status);

/** 每个任务独立监听自己的事件，停止或完成一个任务不会覆盖另一个任务。 */
export function ChatTask({
  task,
  message,
  onControl,
  controlling,
  onTask,
  onHistory,
  onClarification,
  replay,
}: {
  task: Run;
  message?: Message;
  onControl: (
    task: Run,
    action: "pause" | "resume" | "cancel",
  ) => Promise<void>;
  controlling: boolean;
  onTask: (task: Run) => void;
  onHistory: () => Promise<void>;
  onClarification: (task: Run, answer?: string) => void;
  replay: boolean;
}) {
  const [answer, setAnswer] = useState("");
  const [steps, setSteps] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [connection, setConnection] = useState(0);
  const current = useRef(task);
  current.current = task;
  const active = activeTask(task);
  const paused = task.status === "paused";
  const watching = active || replay;
  useEffect(() => {
    if (!watching) return;
    const reader = new AbortController();
    setAnswer("");
    setSteps([]);
    setError("");
    void readEvents(task, reader.signal, (event) => {
      if (reader.signal.aborted) return;
      setSteps((previous) => appendProcessEvent(previous, event));
      if (!active) return;
      if (
        event.type === "run_state" &&
        event.payload.status &&
        event.sequence >= (current.current.event_sequence || 0)
      ) {
        const result = {
          ...current.current,
          status: event.payload.status,
          event_sequence: event.sequence,
        };
        current.current = result;
        onTask(result);
      }
      if (event.type === "checkpoint_restored")
        setAnswer(event.payload.text || "");
      if (event.type === "text_delta")
        setAnswer((text) => text + (event.payload.text || ""));
      if (event.type === "error")
        setError(runError(event.payload.error_code || "执行失败"));
    })
      .then(async () => {
        if (!active || reader.signal.aborted) return;
        const result = await identityRequest<Run>(`/runs/${task.id}`);
        await onHistory();
        if (!reader.signal.aborted) onTask(result);
      })
      .catch((failure) => {
        if (active && !reader.signal.aborted) setError(failure.message);
      });
    return () => reader.abort();
    // 任务对象的用量变化不重开事件流；恢复为活动状态时重新读取该任务。
  }, [task.id, active, connection, watching]);

  return (
    <div className="chat-task" data-run-id={task.id}>
      <ProcessSummary
        steps={
          steps.length ? steps : task.status === "succeeded" ? ["处理完成"] : []
        }
        busy={active && !paused}
      />
      {active && (
        <p className="task-thinking" role="status">
          {paused
            ? "Agent 已暂停思考，点击继续思考后接着执行。"
            : "Agent 正在思考中…你可以继续发送消息。"}
        </p>
      )}
      {active && (
        <div className="task-controls" aria-label="任务控制">
          <button
            type="button"
            disabled={controlling}
            onClick={() => void onControl(task, paused ? "resume" : "pause")}
          >
            {paused ? "继续思考" : "暂停思考"}
          </button>
          <button
            type="button"
            disabled={controlling}
            onClick={() => void onControl(task, "cancel")}
          >
            停止思考
          </button>
        </div>
      )}
      {active && !paused && <ResearchProgress runId={task.id} />}
      {(message || answer) && (
        <article className="message assistant">
          <strong>
            {paused
              ? "YugiTalk · 已暂停"
              : active
                ? "YugiTalk · 生成中"
                : "YugiTalk"}
          </strong>
          <SafeMarkdown>
            {message?.sources?.length || task.knowledge_base_id
              ? withoutSourceLabels(message?.content || answer, active)
              : message?.content || answer}
          </SafeMarkdown>
          <Sources sources={message?.sources || []} />
          <Attachments files={message?.attachments || []} />
        </article>
      )}
      {task.status === "waiting_input" && task.clarification && (
        <aside className="rag-clarification" aria-label="补充信息">
          <p>{task.clarification.prompt}</p>
          {task.clarification.options.map((option) => (
            <button key={option} onClick={() => onClarification(task, option)}>
              {option}
            </button>
          ))}
          <button onClick={() => onClarification(task)}>补充信息</button>
          <button
            disabled={controlling}
            onClick={() => void onControl(task, "cancel")}
          >
            取消本次问答
          </button>
        </aside>
      )}
      {(error || task.error_code) && (
        <p role="alert">
          {error || runError(task.error_code!)}
          {active && (
            <button onClick={() => setConnection((n) => n + 1)}>
              重新连接
            </button>
          )}
        </p>
      )}
    </div>
  );
}

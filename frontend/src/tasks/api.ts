import { monitoredFetch, reportRuntimeFailure } from "../shared/monitoring";

export interface Run {
  id: string;
  status: string;
  error_code: string | null;
  knowledge_base_id?: string | null;
  skill?: {
    name: string;
    number: number;
    version_id: string;
    content_hash: string;
  } | null;
  provider: string;
  model: string;
  charged_tokens: number;
  input_tokens: number | null;
  output_tokens: number | null;
  events_url: string;
  retryable?: boolean;
  attempts?: number;
  checkpoint_node?: string;
  event_sequence?: number;
  state_version?: number;
  clarification?: {
    prompt: string;
    options: string[];
    expires_at: string;
  } | null;
}
export interface RunEvent {
  sequence: number;
  type: string;
  payload: {
    text?: string;
    stage?: string;
    steps?: { title: string; criterion: string; status?: string }[];
    name?: string;
    status?: string;
    error_code?: string;
    skill_name?: string;
    charged_tokens?: number;
  };
}

// 只解析公开事件。断线从最后成功消费的游标续传，既不重新创建任务，也不重复拼接文本。
/** 消费公开事件，断线后从已处理游标重连并丢弃重复序号。 */
export async function readEvents(
  run: Run,
  signal: AbortSignal,
  consume: (event: RunEvent) => void,
) {
  let cursor = "";
  let sequence = 0;
  for (let attempt = 0; attempt < 4 && !signal.aborted; attempt++) {
    try {
      const response = await monitoredFetch(run.events_url, {
        credentials: "same-origin",
        signal,
        headers: cursor ? { "Last-Event-ID": cursor } : {},
      });
      if (!response.ok || !response.body)
        throw new Error(
          response.status === 410
            ? "事件已过期，请查看会话历史。"
            : "事件连接失败，请重新打开会话。",
        );
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      try {
        for (;;) {
          const chunk = await reader.read();
          buffer += decoder
            .decode(chunk.value, { stream: !chunk.done })
            .replace(/\r\n/g, "\n");
          if (buffer.length > 262144) throw new Error("事件数据超出限制。");
          let boundary: number;
          while ((boundary = buffer.indexOf("\n\n")) >= 0) {
            const frame = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);
            const lines = frame.split("\n");
            const id = lines.find((line) => line.startsWith("id: "))?.slice(4);
            const data = lines
              .filter((line) => line.startsWith("data: "))
              .map((line) => line.slice(6))
              .join("\n");
            if (!data || !id) continue;
            const event: RunEvent = JSON.parse(data);
            if (!Number.isInteger(event.sequence) || event.sequence <= sequence)
              continue;
            consume(event);
            sequence = event.sequence;
            cursor = id;
            if (
              (event.type === "completed" ||
                event.type === "error" ||
                event.type === "clarification") &&
              event.sequence >= (run.event_sequence || 0)
            )
              return;
          }
          if (chunk.done) break;
        }
      } finally {
        await reader.cancel().catch(() => undefined);
        reader.releaseLock();
      }
    } catch (failure) {
      if (signal.aborted) return;
      reportRuntimeFailure("events.read", failure);
      if (attempt === 3) throw failure;
    }
    if (attempt === 3) throw new Error("事件流中断，请重新打开会话继续查看。");
    await new Promise<void>((resolve) => {
      /** 结束重连等待并注销取消监听，避免残留定时器。 */
      const finish = () => {
        clearTimeout(timer);
        signal.removeEventListener("abort", finish);
        resolve();
      };
      const timer = setTimeout(finish, 500 * (attempt + 1));
      signal.addEventListener("abort", finish, { once: true });
    });
  }
}

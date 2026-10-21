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
      // 重连只补读游标之后的事件；后台任务不会随这条 HTTP 连接重新执行。
      const separator = run.events_url.includes("?") ? "&" : "?";
      const response = await monitoredFetch(
        `${run.events_url}${separator}types=text_delta,tool_status`,
        {
          credentials: "same-origin",
          signal,
          headers: cursor ? { "Last-Event-ID": cursor } : {},
        },
      );
      if (!response.ok || !response.body)
        throw new Error(
          response.status === 410
            ? "事件已过期，请查看会话历史。"
            : "事件连接失败，请重新打开会话。",
        );
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      // 网络分块与 SSE 事件边界无关：一个事件可能跨多个 chunk，需攒到空行才解析。
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
            // 心跳没有业务数据，会在上面被跳过；重复序号也不能再次追加到页面。
            const event: RunEvent = JSON.parse(data);
            if (!Number.isInteger(event.sequence) || event.sequence <= sequence)
              continue;
            if (event.type !== "cursor") consume(event);
            // 页面成功处理后才推进游标，下一次重连从这里继续。
            sequence = event.sequence;
            cursor = id;
            // 重试任务可能复用旧事件；只有读到本次已知位置之后的结束事件才停止。
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
    // 逐次延长重连间隔；离开页面时通过 abort 提前结束等待。
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

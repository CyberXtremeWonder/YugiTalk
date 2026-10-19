import { identityRequest } from "../identity/api";

export interface TraceRun {
  id: string;
  status: string;
  model: string;
  provider: string;
  created_at: string;
  finished_at: string | null;
  attempts: number;
  charged_tokens: number;
  input_tokens: number | null;
  output_tokens: number | null;
  error_code: string | null;
}

export interface TraceStep {
  id: string;
  parent_id: string | null;
  kind: string;
  name: string;
  iteration: number | null;
  attempt: number;
  status: string;
  started_at: string;
  duration_ms: number | null;
  elapsed_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  details: Record<string, string | number | boolean | null>;
}

export interface TraceDetail {
  run: TraceRun;
  steps: TraceStep[];
  sampled_at: string;
  summary: {
    recorded: boolean;
    model_calls: number;
    loops: number;
    tool_calls: number;
    tool_reuses: number;
    memory_reads: number;
    memory_hits: number;
    retrievals: number;
    input_tokens: number;
    output_tokens: number;
    unknown_usage_calls: number;
    unrecorded_attempts: number;
  };
}

/** 分页读取任务，独立于监控面板的时间窗口。 */
export function fetchTraces(cursor?: string) {
  return identityRequest<{ items: TraceRun[]; next_cursor: string | null }>(
    `/admin/traces${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`,
  );
}

/** 获取持久化明细，运行中的步骤也可立即查看。 */
export function fetchTrace(id: string) {
  return identityRequest<TraceDetail>(
    `/admin/traces/${encodeURIComponent(id)}`,
  );
}

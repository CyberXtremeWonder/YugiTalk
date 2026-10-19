import { ApiError } from "../identity/api";
import { monitoredFetch, reportRuntimeFailure } from "../shared/monitoring";

export interface DiagnosticError {
  kind: "http" | "task" | "document";
  timestamp: string | null;
  code: string;
  route?: string;
  method?: string;
  status?: number;
  request_id?: string;
  trace_id?: string;
  id?: string;
}
export interface Snapshot {
  sampled_at: string;
  window_minutes: number;
  http: {
    started_at: string;
    requests: number;
    server_errors: number;
    client_errors: number;
    error_rate: number;
    average_ms: number;
    trend: {
      timestamp: string;
      requests: number;
      server_errors: number;
      average_ms: number;
    }[];
    routes: {
      method: string;
      route: string;
      requests: number;
      server_errors: number;
      client_errors: number;
      average_ms: number;
      max_ms: number;
    }[];
    errors: DiagnosticError[];
  };
  tasks: {
    charged_tokens: number;
    recent_tokens: number;
    input_tokens: number;
    output_tokens: number;
    unknown_usage_runs: number;
    states: Record<string, number>;
    trend: { timestamp: string; tokens: number }[];
    models: { provider: string; model: string; tokens: number; runs: number }[];
    errors: DiagnosticError[];
  };
}

/** 通过管理员会话读取实时快照，支持页面离开及超时取消。 */
export async function fetchSnapshot(signal: AbortSignal): Promise<Snapshot> {
  let response: Response;
  try {
    response = await monitoredFetch("/api/v1/admin/monitoring", {
      credentials: "same-origin",
      signal,
    });
  } catch {
    throw new ApiError(
      0,
      signal.aborted
        ? "监控请求超时，请检查服务后刷新。"
        : "监控连接中断，请检查网络后刷新。",
    );
  }
  if (!response.ok)
    throw new ApiError(
      response.status,
      response.status === 403
        ? "实时监控仅限管理员访问。"
        : response.status === 401
          ? "登录已过期，请重新登录。"
          : "监控数据暂不可用，请稍后刷新。",
    );
  try {
    return await response.json();
  } catch (failure) {
    reportRuntimeFailure("monitoring.invalid_response", failure);
    throw new ApiError(response.status, "监控响应格式错误，请稍后刷新。");
  }
}

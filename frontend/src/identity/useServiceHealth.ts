import { useEffect, useState } from "react";
import { identityRequest } from "./api";

export interface ServiceStatus {
  environment: string;
  model_provider: string;
  model_id: string;
  embedding_model: string;
  registration_ready: boolean;
  checks: Record<string, boolean>;
  checked_at?: string;
  check_details?: Record<
    string,
    {
      ready: boolean;
      latency_ms: number | null;
      checked_at: string;
      description: string;
    }
  >;
}

/** 健康检查独立刷新；超时、离开页面或旧请求均不能将过期结果显示为当前正常。 */
export function useServiceHealth() {
  const [status, setStatus] = useState<ServiceStatus | null>(null);
  const [error, setError] = useState("");
  const [checking, setChecking] = useState(true);
  const [receivedAt, setReceivedAt] = useState(0);
  const [now, setNow] = useState(Date.now());
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let pending = false;
    const refresh = async () => {
      if (pending || controller.signal.aborted) return;
      clearTimeout(timer);
      pending = true;
      setChecking(true);
      try {
        const result = await identityRequest<ServiceStatus>(
          "/admin/development/status",
          "GET",
          undefined,
          undefined,
          undefined,
          // 后端允许单项探测最多 30 秒，前端留出传输余量以接收完整的失败明细。
          AbortSignal.any([controller.signal, AbortSignal.timeout(35000)]),
        );
        if (
          !result ||
          !result.checks ||
          typeof result.checks !== "object" ||
          Array.isArray(result.checks) ||
          !Object.values(result.checks).every(
            (value) => typeof value === "boolean",
          )
        )
          throw new Error("健康检查返回格式错误。");
        if (!controller.signal.aborted) {
          setStatus(result);
          setReceivedAt(Date.now());
          setNow(Date.now());
          setError("");
        }
      } catch {
        if (!controller.signal.aborted)
          setError("健康检查失败，当前服务状态未知。请检查连接后刷新。");
      } finally {
        pending = false;
        if (!controller.signal.aborted) {
          setChecking(false);
          timer = setTimeout(() => {
            if (document.visibilityState === "visible") void refresh();
          }, 15000);
        }
      }
    };
    const visible = () => {
      if (document.visibilityState === "visible") {
        setNow(Date.now());
        void refresh();
      }
    };
    document.addEventListener("visibilitychange", visible);
    const clock = setInterval(() => setNow(Date.now()), 5000);
    void refresh();
    return () => {
      controller.abort();
      clearTimeout(timer);
      clearInterval(clock);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [revision]);
  return {
    status,
    error,
    checking,
    checkedAt:
      status?.checked_at ||
      (receivedAt ? new Date(receivedAt).toISOString() : ""),
    stale: !!error || (!!receivedAt && now - receivedAt > 45000),
    refresh: () => setRevision((value) => value + 1),
  };
}

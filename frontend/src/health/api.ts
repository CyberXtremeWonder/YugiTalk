import { monitoredFetch } from "../shared/monitoring";

export interface Health {
  status: string;
  checks: Record<string, boolean>;
}

// 就绪接口允许 503；其他协议错误必须明确报告，不能伪装成在线。
/** 请求健康接口并校验返回结构，超时或异常时返回明确错误。 */
export async function fetchHealth(signal?: AbortSignal): Promise<Health> {
  const response = await monitoredFetch("/health/ready", { signal });
  if (!response.ok && response.status !== 503)
    throw new Error("服务状态请求失败");
  const data: unknown = await response.json();
  if (
    !data ||
    typeof data !== "object" ||
    !("status" in data) ||
    !("checks" in data) ||
    typeof data.status !== "string" ||
    !data.checks ||
    typeof data.checks !== "object" ||
    Array.isArray(data.checks) ||
    !Object.values(data.checks).every((value) => typeof value === "boolean")
  ) {
    throw new Error("服务状态响应格式错误");
  }
  return {
    status: data.status,
    checks: data.checks as Record<string, boolean>,
  };
}

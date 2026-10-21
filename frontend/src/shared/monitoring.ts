const routeParts = new Set(
  "api v1 auth login logout me register verification-code accounts members agents versions publish conversations messages runs events cancel retry clarify knowledge-bases documents upload resume memory preferences skills tools submissions approve reject health ready live models audit development users admin monitoring".split(
    " ",
  ),
);

/** 仅保留已知路由片段，移除查询参数、资源编号和文件名。 */
export function diagnosticRoute(path: string): string {
  return path
    .split(/[?#]/, 1)[0]
    .split("/")
    .map((part) => (part === "" || routeParts.has(part) ? part : ":id"))
    .join("/");
}

/** 只记录固定错误类别，禁止记录异常消息、堆栈或用户内容。 */
export function reportRuntimeFailure(operation: string, error: unknown): void {
  const errorType =
    error instanceof TypeError
      ? "TypeError"
      : error instanceof SyntaxError
        ? "SyntaxError"
        : error instanceof RangeError
          ? "RangeError"
          : "Error";
  console.error("[YugiTalk]", { operation, error_type: errorType });
}

/** 记录各模块请求的状态、耗时和关联标识，不采集请求或响应正文。 */
export async function monitoredFetch(
  path: string,
  options?: RequestInit,
): Promise<Response> {
  const started = performance.now();
  const route = diagnosticRoute(path);
  const method = options?.method || "GET";
  let response: Response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    if (!options?.signal?.aborted)
      console.error("[YugiTalk]", {
        operation: "http.request",
        route,
        method,
        status_code: 0,
        error_code: "network_error",
        duration_ms: Math.round(performance.now() - started),
      });
    throw error;
  }
  const record = {
    // fetch 在响应头到达时结束，这里的耗时不是 SSE 全文生成时间；不读取或复制响应正文。
    operation: "http.request",
    route,
    method,
    status_code: response.status,
    duration_ms: Math.round(performance.now() - started),
    request_id: response.headers.get("X-Request-ID") || "",
    trace_id: response.headers.get("X-Trace-ID") || "",
  };
  if (response.status >= 500) console.error("[YugiTalk]", record);
  else if (!response.ok) console.warn("[YugiTalk]", record);
  else console.debug("[YugiTalk]", record);
  return response;
}

/** 注册全局监听，捕获事件处理器和未处理异步错误。 */
export function installRuntimeMonitoring(): void {
  window.addEventListener("error", (event) =>
    reportRuntimeFailure("runtime.error", event.error),
  );
  window.addEventListener("unhandledrejection", (event) =>
    reportRuntimeFailure("runtime.unhandled_rejection", event.reason),
  );
}

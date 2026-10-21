import { monitoredFetch, reportRuntimeFailure } from "../shared/monitoring";

export interface Identity {
  user: { id: string; email: string; name: string; platform_admin: boolean };
  csrf_token: string;
  development_mode?: boolean;
}

/** 携带接口状态码和可公开提示，供页面区分失效会话与业务错误。 */
export class ApiError extends Error {
  /** 保存接口状态码与公开提示，并初始化标准错误信息。 */
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

// 列表按游标逐页读取，每个请求有明确上限，避免服务端返回无界结果。
/** 按有界游标逐页读取列表，并拒绝重复游标造成的无限循环。 */
export async function allItems<T>(path: string): Promise<T[]> {
  const items: T[] = [];
  let cursor: string | null = null;
  const seen = new Set<string>();
  do {
    const page: { items: T[]; next_cursor: string | null } =
      await identityRequest(
        `${path}?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
      );
    items.push(...page.items);
    // 当前页消费后再更新游标；重复游标意味着服务端没有向前推进，必须停止续页。
    cursor = page.next_cursor;
    if (cursor && seen.has(cursor))
      throw new Error("列表游标无效，请刷新页面。");
    if (cursor) seen.add(cursor);
  } while (cursor);
  return items;
}

/** 加载登录用户、所属空间和防伪令牌，补齐空间分页结果。 */
export async function fetchIdentity(): Promise<Identity> {
  const identity = await identityRequest<Identity>("/auth/me");
  return identity;
}

// 会话仅在 HttpOnly Cookie 中，CSRF Token 只保存在内存，不使用浏览器持久存储。
/** 通过同源会话调用接口，写请求携带防伪令牌并统一处理错误。 */
export async function identityRequest<T>(
  path: string,
  method = "GET",
  body?: unknown,
  csrf?: string,
  headers?: Record<string, string>,
): Promise<T> {
  let response: Response;
  try {
    response = await monitoredFetch(`/api/v1${path}`, {
      method,
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        ...(csrf ? { "X-CSRF-Token": csrf } : {}),
        ...headers,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    // 网络中断没有 HTTP 状态码，统一提供可操作的中文提示。
    throw new ApiError(0, "连接已中断，请检查网络或 SSH 隧道，然后重试。");
  }
  if (!response.ok) {
    // 先按 HTTP 状态提供通用提示，再用白名单中的业务码细化；不直接展示响应正文。
    let message =
      response.status === 401
        ? path === "/auth/login"
          ? "邮箱、账号或密码不正确"
          : "登录信息无效或会话已过期。"
        : response.status === 429
          ? "尝试过于频繁，请稍后重试。"
          : response.status === 403
            ? "没有操作权限或请求校验失败。"
            : response.status === 409
              ? "操作与当前状态冲突，请刷新后重试。"
              : response.status === 404
                ? "资源不存在或没有访问权限。"
                : "请求失败，请检查服务状态。";
    // 仅识别约定的业务错误码，不展示服务端任意错误文本。
    const businessErrors: Record<string, { status: number; message: string }> =
      {
        tool_review_required: {
          status: 403,
          message: "用户工具必须提交管理员审核后发布。",
        },
        tool_submission_limit: {
          status: 409,
          message: "待审工具已达20个，请等待审核。",
        },
        tool_submission_reviewed: {
          status: 409,
          message: "该提交已完成审核，请刷新列表。",
        },
        tool_submitter_unavailable: {
          status: 409,
          message: "提交者账号已停用，不能批准工具。",
        },
        email_already_registered: {
          status: 409,
          message: "该邮箱已注册，请直接登录。",
        },
        verification_code_invalid: {
          status: 422,
          message: "验证码无效、已过期或尝试次数过多，请重新获取。",
        },
        registration_mail_unavailable: {
          status: 503,
          message: "注册邮件服务尚未配置或暂不可用，请联系管理员。",
        },
        registration_disabled: { status: 403, message: "当前服务未开放注册。" },
        development_admin_required: {
          status: 403,
          message: "开发模式和管理员面板仅限管理员访问。",
        },
        admin_account_protected: {
          status: 409,
          message: "管理员账号不能通过启停面板停用。",
        },
        member_already_exists: {
          status: 409,
          message:
            "该用户已是当前工作空间成员，无需重复添加。修改权限请使用下方成员列表。",
        },
        member_user_unavailable: {
          status: 404,
          message: "用户不存在或已停用，请先由管理员初始化账号，再添加成员。",
        },
        last_account_owner: {
          status: 409,
          message: "不能移除或降级工作空间的最后一位所有者。",
        },
        account_active_tasks: {
          status: 409,
          message: "空间内有未结束任务，请先停止或完成任务后再删除。",
        },
        memory_disabled: {
          status: 409,
          message: "长期记忆已关闭，请先开启再保存或修改偏好。",
        },
        memory_content_invalid: {
          status: 422,
          message: "请确认保存偏好，内容不能包含密码、令牌或密钥。",
        },
        knowledge_document_limit: {
          status: 409,
          message: "单个知识库最多支持 200 份有效文档。",
        },
        run_budget_insufficient: {
          status: 429,
          message:
            "剩余额度不足。请在 Agent 页降低单次任务预算并发布新版本，或联系管理员调整总额度。",
        },
        run_concurrency_exceeded: {
          status: 429,
          message: "正在执行的任务已达上限，请等待完成或停止已有任务。",
        },
        model_configuration_changed: {
          status: 409,
          message: "模型配置已改变，请在 Agent 页重新发布助手版本。",
        },
        skill_unavailable: {
          status: 409,
          message: "Skill 已停用或删除，请重新选择。",
        },
        skill_content_changed: {
          status: 409,
          message: "Skill 版本校验失败，请重新选择或联系管理员。",
        },
        skill_name_conflict: {
          status: 409,
          message: "当前空间已存在同名 Skill，请换一个名称。",
        },
        skill_catalog_limit: {
          status: 409,
          message: "当前空间最多支持 100 个有效 Skill。",
        },
        skill_version_limit: {
          status: 409,
          message: "单个 Skill 最多发布 100 个版本。",
        },
        tool_name_conflict: {
          status: 409,
          message: "当前空间已存在同名 Tool，内置名称不能覆盖。",
        },
        tool_version_limit: {
          status: 409,
          message: "单个 Tool 最多发布 100 个版本。",
        },
        tool_catalog_limit: {
          status: 409,
          message: "当前空间最多支持 100 个自定义 Tool。",
        },
        tool_not_registered: {
          status: 422,
          message: "授权列表包含不存在的工具，请刷新目录并取消旧授权。",
        },
        tool_name_immutable: {
          status: 422,
          message: "工具标识和内置实现不能修改。",
        },
        builtin_tool_not_deletable: {
          status: 409,
          message: "内置工具只能停用，不能删除。",
        },
        tool_arguments_invalid: {
          status: 422,
          message: "示例参数不符合已发布的工具结构。",
        },
        tool_unavailable: {
          status: 409,
          message: "工具已停用或删除，请重新选择。",
        },
        tool_content_changed: {
          status: 409,
          message: "工具版本校验失败，请联系管理员。",
        },
        tool_not_configured: {
          status: 409,
          message: "工具配置尚未就绪，请检查 Tool 页面。",
        },
        tool_not_authorized: {
          status: 409,
          message: "当前任务没有此工具的调用权限。",
        },
      };
    const payload: unknown = await response.json().catch(() => null);
    if (
      payload &&
      typeof payload === "object" &&
      "code" in payload &&
      typeof payload.code === "string" &&
      Object.hasOwn(businessErrors, payload.code)
    ) {
      const business = businessErrors[payload.code];
      if (business.status === response.status) message = business.message;
    }
    if (
      response.status === 401 &&
      path !== "/auth/login" &&
      path !== "/auth/me"
    )
      // 业务请求发现会话失效时通知 App；登录与首次身份探测由各自页面处理，避免重复提示。
      window.dispatchEvent(new Event("session-expired"));
    throw new ApiError(response.status, message);
  }
  // 删除、退出等接口没有响应正文，不能继续按 JSON 解析。
  if (response.status === 204) return undefined as T;
  try {
    return (await response.json()) as T;
  } catch (error) {
    reportRuntimeFailure("http.invalid_response", error);
    throw new ApiError(response.status, "服务响应格式错误，请重试。");
  }
}

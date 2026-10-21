/** 将公开任务错误码转换为中文提示，不展示供应商原始错误内容。 */
export function runError(code: string): string {
  const messages: Record<string, string> = {
    event_limit:
      "任务事件达到上限，输出已中断并保留部分内容，请缩小问题范围后重试。",
    model_repetition:
      "检测到模型连续重复输出，已停止生成并保留部分内容，请缩小问题范围后重试。",
    invalid_citation: "回答包含无法核验的来源引用，任务已停止，请重试。",
    agent_incomplete:
      "检查后仍未满足任务要求，请根据缺口补充信息或缩小任务范围。",
    agent_control_invalid: "模型未返回有效的计划或检查结果，请重新发送。",
    agent_checkpoint_invalid: "任务恢复状态无效，请重新创建任务。",
    model_step_limit: "已达到模型调用上限。规划、执行和检查均计入此上限。",
    tool_call_limit:
      "已达到工具调用上限，模型未能基于已有结果完成收尾，请缩小问题范围后重试。",
    token_budget_exhausted: "已达到任务预算，请调整预算或缩小任务范围。",
    model_output_limit:
      "回复达到本次调用的 Token 上限，已保留部分内容。请缩小问题范围，或检查 Agent 输出配置并发布新版本；内部调用有独立上限。",
    model_incomplete: "模型响应提前结束，已保留部分内容，请重新发送。",
    model_stream_size_limit:
      "模型响应流超过大小保护上限，已保留部分内容，请缩小问题范围或检查流容量配置。",
    model_response_too_large:
      "模型正文或工具参数超过大小保护上限，已保留部分内容，请缩小问题范围。",
    model_timeout_or_network: "模型连接超时或中断，请稍后重试。",
    model_transient: "模型服务暂时繁忙，请稍后重试。",
    model_authentication: "模型服务鉴权失败，请检查模型密钥与访问权限。",
    model_request_rejected: "模型服务拒绝请求，请检查模型名称及服务配置。",
    model_configuration: "模型配置不完整，请检查服务地址、模型名称和密钥。",
    rag_model_request_failed: "知识库评分调用失败，请检查模型服务后重试。",
    rag_model_protocol: "知识库评分响应格式异常，请检查模型服务兼容性。",
    rag_structured_output_invalid:
      "知识库评分未返回有效结果，请重新发送或调整评分模型。",
    rag_deadline: "知识库处理超时，请缩小问题范围或稍后重试。",
    execution_error: "任务执行失败，请查看服务日志后重试。",
    skill_unavailable: "所选 Skill 已停用或删除，请重新选择。",
    skill_content_changed: "Skill 版本校验失败，请重新选择或联系管理员。",
    tool_unavailable: "工具已停用或删除，请检查 Tool 配置后重新发送。",
    tool_content_changed: "工具版本校验失败，请联系空间管理员。",
    tool_not_authorized: "当前任务没有此工具的调用权限。",
    tool_not_configured: "工具尚未配置就绪，请在 Tool 页面查看原因。",
    tool_network_denied: "工具目标未通过公共网络检查。",
    tool_dns_timeout: "工具域名解析超时，请稍后重试。",
    tool_dns_busy: "工具域名解析繁忙，请稍后重试。",
    tool_timeout: "工具查询超时，请稍后重试。",
    tool_network_failed: "工具服务连接失败，请检查服务配置。",
    cancelled: "任务已停止。",
  };
  return messages[code] || "任务未完成，请查看任务记录或服务日志。";
}

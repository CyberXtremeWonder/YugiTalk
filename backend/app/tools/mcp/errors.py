"""可公开的 MCP 原因与处理建议，不包含远端响应体或凭证。"""

MCP_ERRORS = {
    "mcp_transport_mismatch": (
        422,
        "传输类型不匹配：服务器不接受当前连接方式。"
        "请核对提供商说明，切换 SSE 或 Streamable HTTP，保存后重试。",
    ),
    "mcp_auth_required": (
        422,
        "服务器认证失败。请检查 Bearer Token 是否正确、过期或缺少访问权限；"
        "需要 OAuth 的服务暂不支持。",
    ),
    "mcp_not_found": (
        422,
        "服务器返回 404，MCP 地址不存在。请填写提供商给出的完整 MCP 地址，不能只填网站首页。",
    ),
    "mcp_http_failed": (
        422,
        "远端服务器拒绝连接。请检查 MCP 地址、服务运行状态及提供商的访问限制。",
    ),
    "mcp_timeout": (422, "等待 MCP 服务器响应超时。请检查网络和远端服务状态，稍后重新测试连接。"),
    "mcp_network_failed": (422, "无法建立 MCP 网络连接。请检查域名、网络和 HTTPS 证书是否正常。"),
    "mcp_connection_closed": (422, "MCP 连接提前关闭，未收到完整响应。请核对传输类型并稍后重试。"),
    "mcp_response_invalid": (
        422,
        "返回内容不是有效的 MCP 响应。请确认填写的是 MCP 端点，并核对传输类型。",
    ),
    "mcp_protocol_unsupported": (
        422,
        "服务器协商的 MCP 协议版本暂不支持。当前支持 2024-11-05 至 2025-11-25 的初始化协议。",
    ),
    "mcp_tools_unsupported": (
        422,
        "连接成功，但服务器未声明工具能力。请选择支持 tools 的 MCP 服务。",
    ),
    "mcp_rpc_failed": (
        422,
        "服务器拒绝了 MCP 初始化或工具发现请求。请检查服务配置和远端运行记录。",
    ),
    "mcp_tools_invalid": (
        422,
        "工具声明无法解析或包含不支持的参数 Schema。"
        "请联系服务提供商检查声明，外部 Schema 引用暂不支持。",
    ),
    "mcp_endpoint_denied": (
        422,
        "服务器返回的消息地址与配置地址不同源，已阻止连接。请使用同源 MCP 地址或联系服务提供商。",
    ),
    "mcp_response_too_large": (
        422,
        "MCP 响应超过 1 MiB 限制。请减少该服务返回的工具数量或响应大小。",
    ),
    "mcp_tool_limit": (422, "MCP 工具列表超过数量或分页上限。请使用工具数量不超过 100 项的服务。"),
    "mcp_discovery_failed": (422, "MCP 连接或工具发现失败。请检查服务器地址、传输类型和认证配置。"),
    "tool_network_denied": (
        422,
        "目标解析到本机、私网或非公共地址，已阻止连接。请使用公共 HTTPS MCP 地址。",
    ),
    "tool_dns_timeout": (422, "MCP 域名解析超时。请检查 DNS 和网络，稍后重试。"),
    "tool_dns_busy": (422, "域名解析服务繁忙。请稍后重新测试连接。"),
    "tool_network_failed": (422, "无法解析 MCP 域名。请检查地址拼写和 DNS 配置。"),
    "mcp_new_endpoint_requires_token": (
        422,
        "修改地址时请重新填写 Token 或明确清除旧 Token，防止凭证被发送到其他服务器。",
    ),
    "mcp_server_limit": (409, "每个用户最多添加 10 个有效 MCP 服务器。请先删除不再使用的服务器。"),
    "mcp_disabled": (409, "服务器已停用。请先启用再测试连接。"),
    "mcp_configuration_changed": (409, "测试期间配置发生变化。请保存最新配置后重新测试。"),
    "mcp_version_limit": (409, "服务器配置已达 100 个版本。请创建新的服务器配置。"),
    "mcp_name_required": (422, "服务器名称不能为空，请填写名称。"),
}

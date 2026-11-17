# 当前 API 入口与关键契约

更新日期：2026 年 11 月 13 日。下表由当前 `create_app()` 生成的 OpenAPI 路由清单核对，参数和响应 Schema 以源码及开发环境 `/docs` 为准。生成清单未启动 lifespan、未发送模型请求或修改业务数据。生产通常关闭调试文档。

## 身份 权限与通用约束

- 资源归属由会话推导，用户只操作本人资料。旧组织工作空间和成员接口已移除；`/me/workspace` 仅保留文件目录读取兼容。
- 受保护接口使用会话 Cookie；写请求按当前依赖校验 Origin 与 `X-CSRF-Token`。注册和登录采用各自校验及限流，管理员接口另查平台管理员权限。
- 分页列表使用 `limit` 和 `cursor`，实际 Schema 说明各接口支持项。不存在或无权访问资源通常返回 404，不向客户端泄露内部异常和密钥。
- 登录按完整密码验证，不做首尾空格清理；没有默认管理员密码。SMTP 注册和工具发布审核见 [身份说明](REGISTRATION_AND_TOOL_REVIEW.md)。

## 创建与控制任务

`POST /api/v1/runs` 需要 `Idempotency-Key`。请求包含 `conversation_id`、`agent_version_id` 和最多 6000 字符的 `message`，可选 `skill_version_id`、`uploaded_files`、`research_project_id` 及兼容的 `knowledge_base_id`。

相同键与内容重试返回原任务，同键换内容冲突。提交事务保存消息、资源快照及排队意图后再投递。同一会话允许多个任务，但用户和账号并发上限仍生效。预算保护可配置关闭，步数、超时和权限等限制独立生效。

| 操作 | 关键约束 |
| --- | --- |
| pause | queued 或 running 进入 paused；重复请求不重复推进 |
| resume | paused 优先恢复原栈，无有效租约时从检查点排队 |
| cancel | 取消活动任务，保留已公开正文并结算 |
| retry | 仅可恢复的失败任务，新的幂等请求；可能重新生成并计费 |
| clarify | waiting_input，`answer` 最多 1000 字符并携带当前 `state_version`；成功从检索继续 |
| steer | queued、running 或 paused，`message` 最多 6000 字符，每 Run 最多 20 条；不自动继续暂停任务，不支持深度研究 |

clarify 和 steer 均要求 `Idempotency-Key`，不能互相替代。完整时序、错误与当前支持范围见 [HITL](HITL.md)。Run 公开状态包含版本、事件游标、恢复提示及 `steering_supported`，不返回内部检查点和凭证。

## SSE 与文件

`GET /runs/{run_id}/events` 读取持久公开事件，使用 `Last-Event-ID` 按序号续传；可选 types 过滤，但完成、错误、澄清、状态及检查点恢复等控制事件始终保留。过滤事件仍推进游标；关闭页面不取消任务。详见 [OBSERVABILITY](OBSERVABILITY.md)。

知识库上传使用原始请求体和 filename 查询参数，JSONL 最大 5 GiB，其他文档最大 20 MiB；不等于聊天附件限制。聊天附件最多 3 个，单个 20 MiB，提取正文各有独立上限。下载接口再次检查用户归属和来源状态，不能把模型输出地址当作下载权限。详见 [分块](DOCUMENT_CHUNKING.md)、[附件](CHAT_UPLOADS.md) 和 [研究交付](DEEP_RESEARCH.md)。

## 当前路由清单

### 健康

| 方法 | 完整路径 |
| --- | --- |
| GET | `/health/live` |
| GET | `/health/ready` |

### 身份与账号

| 方法 | 完整路径 |
| --- | --- |
| POST | `/api/v1/auth/development-mode` |
| POST | `/api/v1/auth/login` |
| POST | `/api/v1/auth/logout` |
| GET | `/api/v1/auth/me` |
| GET | `/api/v1/auth/options` |
| POST | `/api/v1/auth/register` |
| POST | `/api/v1/auth/registration-code` |

### Agent

| 方法 | 完整路径 |
| --- | --- |
| DELETE | `/api/v1/agents/{agent_id}` |
| POST | `/api/v1/agents/{agent_id}/versions` |
| GET | `/api/v1/me/agents` |
| POST | `/api/v1/me/agents` |
| GET | `/api/v1/model/capabilities` |

### 对话与附件

| 方法 | 完整路径 |
| --- | --- |
| POST | `/api/v1/chat/uploads` |
| PATCH | `/api/v1/conversations/{conversation_id}` |
| DELETE | `/api/v1/conversations/{conversation_id}` |
| GET | `/api/v1/conversations/{conversation_id}/export` |
| GET | `/api/v1/conversations/{conversation_id}/messages` |
| GET | `/api/v1/conversations/{conversation_id}/runs` |
| GET | `/api/v1/me/conversations` |
| POST | `/api/v1/me/conversations` |

### 任务与人工介入

| 方法 | 完整路径 |
| --- | --- |
| GET | `/api/v1/me/runs` |
| POST | `/api/v1/runs` |
| GET | `/api/v1/runs/{run_id}` |
| POST | `/api/v1/runs/{run_id}/cancel` |
| POST | `/api/v1/runs/{run_id}/clarify` |
| GET | `/api/v1/runs/{run_id}/events` |
| POST | `/api/v1/runs/{run_id}/pause` |
| POST | `/api/v1/runs/{run_id}/resume` |
| POST | `/api/v1/runs/{run_id}/retry` |
| POST | `/api/v1/runs/{run_id}/steer` |

### 知识库与来源

| 方法 | 完整路径 |
| --- | --- |
| DELETE | `/api/v1/documents/{document_id}` |
| GET | `/api/v1/documents/{document_id}/download` |
| GET | `/api/v1/documents/{document_id}/graph` |
| POST | `/api/v1/documents/{document_id}/graph` |
| POST | `/api/v1/documents/{document_id}/retry` |
| DELETE | `/api/v1/knowledge-bases/{base_id}` |
| GET | `/api/v1/knowledge-bases/{base_id}/documents` |
| POST | `/api/v1/knowledge-bases/{base_id}/documents` |
| POST | `/api/v1/knowledge-bases/{base_id}/search` |
| GET | `/api/v1/me/knowledge-bases` |
| POST | `/api/v1/me/knowledge-bases` |
| GET | `/api/v1/sources/{chunk_id}` |

### 记忆

| 方法 | 完整路径 |
| --- | --- |
| GET | `/api/v1/me/memories` |
| POST | `/api/v1/me/memories` |
| PATCH | `/api/v1/me/memory-preference` |
| PATCH | `/api/v1/memories/{memory_id}` |
| DELETE | `/api/v1/memories/{memory_id}` |

### Skill

| 方法 | 完整路径 |
| --- | --- |
| GET | `/api/v1/me/skills` |
| POST | `/api/v1/me/skills` |
| GET | `/api/v1/skills/{skill_id}` |
| PATCH | `/api/v1/skills/{skill_id}` |
| DELETE | `/api/v1/skills/{skill_id}` |
| POST | `/api/v1/skills/{skill_id}/versions` |

### 工具与 MCP

| 方法 | 完整路径 |
| --- | --- |
| GET | `/api/v1/me/mcp-servers` |
| POST | `/api/v1/me/mcp-servers` |
| PUT | `/api/v1/me/mcp-servers/{key}` |
| PATCH | `/api/v1/me/mcp-servers/{key}` |
| DELETE | `/api/v1/me/mcp-servers/{key}` |
| POST | `/api/v1/me/mcp-servers/{key}/discover` |
| POST | `/api/v1/me/tool-submissions` |
| GET | `/api/v1/me/tool-submissions` |
| GET | `/api/v1/me/tools` |
| POST | `/api/v1/me/tools` |
| PATCH | `/api/v1/me/tools/{name}` |
| DELETE | `/api/v1/me/tools/{name}` |
| POST | `/api/v1/me/tools/{name}/validate` |
| POST | `/api/v1/me/tools/{name}/versions` |

### 研究与文件

| 方法 | 完整路径 |
| --- | --- |
| GET | `/api/v1/research` |
| POST | `/api/v1/research` |
| GET | `/api/v1/research/runs/{run_id}/progress` |
| GET | `/api/v1/research/{project_id}` |
| GET | `/api/v1/research/{project_id}/files/{format}` |

### 旧目录读取兼容

| 方法 | 完整路径 |
| --- | --- |
| GET | `/api/v1/me/workspace` |
| PUT | `/api/v1/me/workspace` |
| GET | `/api/v1/me/workspace/directories` |
| POST | `/api/v1/me/workspace/import` |

### 管理员观测与审核

| 方法 | 完整路径 |
| --- | --- |
| GET | `/api/v1/admin/audit` |
| GET | `/api/v1/admin/development/status` |
| GET | `/api/v1/admin/monitoring` |
| GET | `/api/v1/admin/tool-submissions` |
| POST | `/api/v1/admin/tool-submissions/{submission_id}/review` |
| GET | `/api/v1/admin/traces` |
| GET | `/api/v1/admin/traces/{run_id}` |
| GET | `/api/v1/admin/usage` |
| GET | `/api/v1/admin/usage/balances` |
| GET | `/api/v1/admin/users` |
| PATCH | `/api/v1/admin/users/{user_id}` |

## 公开范围与兼容边界

指标等 `include_in_schema=False` 的内部端点不在上表；它们需要独立认证，不能从公网开放。健康检查、测试通过或模型协议替身不代表真实生成质量。

工具发布审核是管理员审批工具版本，不是每次执行前人工审批。工单及配套审批业务入口已下线，历史表和旧版本兼容逻辑仍保留。旧目录接口仍注册不代表当前前端还提供文件工作空间设置。

后端契约主要位于各模块的 contracts 和 router；全调用顺序见 [PROJECT_FLOW](PROJECT_FLOW.md)，维护入口见 [文档导航](README.md)。

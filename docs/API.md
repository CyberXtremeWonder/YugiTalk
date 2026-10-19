# API 契约设计 v0.1

状态：阶段 1–5 的健康、身份、空间、Agent、私有会话、运行事件、知识库、记忆与执行恢复接口已实现，已由用户验收；阶段 6 开发中。七步增强 RAG 与澄清恢复已实现，自动验证通过，本轮人工验收待确认。

登录密码不限制字符长度，前端不截断、后端不按长度拒绝输入；错误密码返回认证失败。初始化用户时也不设长度上下限，但密码不能为空。密码不做首尾空格清理，按完整内容进行 Argon2id 验证。

## 阶段 5 已实现可靠任务与恢复

| 方法与路径（前缀 `/api/v1`） | 行为 |
| --- | --- |
| POST `/runs/{id}/retry` | 新 `Idempotency-Key`；仅可恢复失败，同一键重复请求返回同一运行，可能产生新模型费用 |

运行新增 `state_version`、`attempts`、`checkpoint_node`、`event_sequence`、`retryable` 与恢复成本说明；不暴露检查点上下文、租约令牌或模型凭证。同会话不允许创建第二个活动任务；恢复时重新检查权限、预算和并发。

创建工单工具、工单列表和审批接口已下线，相关路径返回 404；新助手配置包含 create_ticket 时返回 422。旧版本只在响应及运行时过滤该工具，数据库版本保持不可变。旧等待审批任务由 Scheduler 以 tool_removed 结束并释放预算。历史工单数据保留，项目全部完成后再另行扩展。

## 阶段 4 已实现知识库与记忆接口

路径均有 `/api/v1` 前缀。接口校验登录和空间成员资格；写入使用 Cookie、受信 Origin 与 `X-CSRF-Token`。知识库管理要求 owner/admin，检索与引用允许成员；记忆仅允许本人操作。

| 方法与路径 | 行为 |
| --- | --- |
| GET/POST `/workspaces/{id}/knowledge-bases` | 分页列出、创建知识库 |
| GET `/knowledge-bases/{id}/documents` | 分页文档及异步索引状态、progress |
| POST `/knowledge-bases/{id}/documents?filename=名称.md` | 原始字节请求体上传，最大 20 MB；202，重复哈希不重复索引 |
| POST `/knowledge-bases/{id}/search` | JSON `{query}`，仅检索授权且可用的当前索引版本 |
| POST `/documents/{id}/retry` | 重建或重试索引；删除失败重试清理 |
| DELETE `/documents/{id}` | 202，立即停止召回，后台清理 |
| GET `/sources/{chunk_id}` | 来源片段、文件与页码/分块；失效来源返回 404 |
| GET `/documents/{id}/download` | 授权下载原文 |
| GET/POST `/workspaces/{id}/memories` | 本人偏好；保存要求 `confirmed: true` |
| PATCH `/workspaces/{id}/memory-preference` | `{enabled}`，关闭后不读写或注入偏好 |
| PATCH/DELETE `/memories/{id}` | 修改需要重新确认；删除仅限本人 |

`POST /runs` 新增可选 `knowledge_base_id`；会话消息新增 `sources`，保存来源元数据。查询无证据时说明资料不足，不发起模型调用。外部嵌入服务使用 OpenAI 兼容 `/v1/embeddings`，与聊天 API 独立。

## 阶段 2 已实现身份与空间接口

以下路径均有 `/api/v1` 前缀。写操作须携带受信 Origin，登录之外还须 Cookie 与 `X-CSRF-Token`。响应禁止缓存；不返回密码哈希或会话令牌。

| 方法与路径 | 行为与权限 | 成功 |
| --- | --- | --- |
| POST `/auth/login` | email／password；Redis 原子限流，无默认账号 | 200，user、workspaces、workspace_next_cursor、csrf_token；设置 Cookie |
| GET `/auth/me` | 校验用户活跃状态与会话到期 | 200，同上 |
| POST `/auth/logout` | 撤销当前服务端会话，删除 Cookie | 204 |
| GET `/workspaces` | 只列出当前用户成员空间；limit／cursor | 200，items／next_cursor |
| POST `/workspaces` | name，当前用户成为 owner | 201，id／name／role |
| GET `/workspaces/{id}` | 必须为成员，未授权或不存在返回 404 | 200，id／name／role |
| GET `/workspaces/{id}/members` | owner／admin；limit／cursor | 200，items／next_cursor |
| POST `/workspaces/{id}/members` | 已初始化用户邮箱，role=admin／member；重复加入 409 | 201，id／role |
| PATCH `/workspaces/{id}/members/{user_id}` | role=owner／admin／member；admin 不可授予或管理 owner | 200，id／role |
| DELETE `/workspaces/{id}/members/{user_id}` | owner／admin；禁止移除最后一个 owner | 204 |
| GET `/admin/audit` | 平台管理员；只读元数据，按时间和 ID 倒序；limit／cursor | 200，items／next_cursor |

成员操作锁定空间行再校验权限，最后 owner 冲突返回 409。所有列表默认 limit=20、最大 100，UUID 游标；登录身份快照最多返回 100 个空间，客户端通过工作空间列表继续读取。失效或撤销会话 401，无管理权限／来源或 CSRF 不匹配 403。登录限流 429，Retry-After=900；Redis 故障拒绝登录返回 503。

成员业务错误码：重复加入为 `member_already_exists`（409），账号未初始化或停用为 `member_user_unavailable`（404），最后所有者不能移除或降级为 `last_workspace_owner`（409）。这些提示仅在调用者已通过空间管理权限校验后返回；其他跨空间查询仍使用通用 404。

## 阶段 3 已实现 Agent 接口

以下均以 `/api/v1` 为前缀；写入遵循已有 Cookie、Origin、CSRF 契约。

| 方法与路径 | 行为 |
| --- | --- |
| GET `/model/capabilities` | 当前供应商／模型、是否配置与离线说明，不返回凭证 |
| GET/POST `/workspaces/{id}/agents` | 成员可读取；owner／admin 创建，返回 id、version_id、number |
| POST `/agents/{id}/versions` | owner／admin 发布不可变版本；工具与限额严格校验 |
| GET/POST `/workspaces/{id}/conversations` | 本人会话列表／创建 |
| PATCH/DELETE `/conversations/{id}` | 本人重命名／删除会话、消息和运行事件；有活动运行时删除返回 409 |
| GET `/conversations/{id}/messages` | 本人历史；limit ≤100，cursor 为消息序号 |
| POST `/runs` | 必须有 Idempotency-Key；输入 conversation_id、agent_version_id、message，202 返回 id、status、events_url 与用量字段 |
| GET `/runs/{id}` | 仅本人且仍为工作空间成员；管理员也不能读取他人私有会话 |
| GET `/runs/{id}/events` | Cookie 鉴权，Last-Event-ID=`RunID:sequence`，非法游标 422，创建时间超过保留期 410 |
| POST `/runs/{id}/cancel` | 本人显式取消；终态重复请求不重复结算 |
| GET `/conversations/{id}/runs` | 本人会话最近 20 个运行，用于回到会话后恢复状态读取 |
| GET `/workspaces/{id}/runs` | 本人在空间中的运行列表，limit／UUID cursor |

创建任务幂等作用域为用户；同键同参数返回原运行，不重复追加消息或预留额度，不同参数 409。键随运行保存至会话删除；删除后不应重试原创建请求。

状态为 queued／running／succeeded／failed／cancelled。最多 12 次模型决策、20 次授权工具调用、300 秒执行配置时长。默认单用户并发 2、空间并发 4，各有累计 100000 保守额度。额度耗尽或并发超限返回 429，配置变化返回 `model_configuration_changed`（409）。预算额度与供应商实际 Token 分开记录，Mock／未知用量为空，费用未知为 null。

## 阶段 1 已实现接口

| 方法与路径 | 成功 | 不可用 |
| --- | --- | --- |
| GET `/` | 307，开发跳转 `/docs`，生产跳转 `/health/live` | 后端入口不承载前端工作台 |
| GET `/health/live` | 200，`{"status":"ok","checks":{}}` | 不检查外部依赖 |
| GET `/health/ready` | 200，status=ready；checks 包含 postgres、redis、storage、milvus 的布尔状态 | 503，status=unavailable，并标出失败依赖 |

响应提供服务端生成的 `X-Request-ID`；未知路径 404、内部错误 500 使用 `{code,message,request_id,details}` 脱敏结构。开发提供 `/docs` 和 `/openapi.json`，生产关闭。健康路径不使用 `/api/v1` 前缀；业务前缀约定如下。

## 全局约定

- 前缀 `/api/v1`；认证采用 HttpOnly Cookie，修改操作校验 CSRF 和来源。
- 登录成功返回用户、空间列表与 CSRF Token；会话令牌不返回给 JavaScript，不放入 URL。
- 时间为 UTC ISO 8601；列表用 `cursor` 与 `limit`（默认 20，最大 100），响应 `{items,next_cursor}`。
- 身份由服务端会话确认；空间 ID 不能代替权限验证。未登录 401，无角色权限 403，跨空间资源查询使用 404 避免泄露存在性。
- 统一错误 `{code,message,request_id,details}`，`details` 只含脱敏信息。校验失败 422，幂等参数冲突 409，额度耗尽 429，依赖不可用 503。
- 创建运行要求 `Idempotency-Key`；同一用户与空间内 24 小时有效。相同键和请求返回原运行，参数不一致返回 409。

## 全项目目标接口（包含后续阶段设计）

| 方法与路径 | 输入/行为 | 返回 |
| --- | --- | --- |
| POST `/auth/login` | email、password；失败限流 | 用户、空间与 CSRF Token，设置会话 Cookie |
| GET `/auth/me` | 校验会话 | 当前用户、权限与 CSRF Token |
| POST `/auth/logout` | 清除服务端会话 | 204 |
| GET `/workspaces` | 查询成员空间 | 空间列表 |
| GET/POST `/workspaces/{id}/agents` | 管理 Agent | 列表/201 |
| POST `/agents/{id}/versions` | 提交配置与版本 | 不可变版本 |
| GET/POST `/workspaces/{id}/conversations` | 管理本人会话 | 列表/201 |
| GET `/conversations/{id}/messages` | 分页历史 | 消息列表 |
| POST `/runs` | conversation_id、agent_version_id、message，幂等键 | 202，run_id、status、events_url |
| GET `/runs/{id}` | 本人运行或授权管理者 | 状态、步骤摘要、结果、错误 |
| GET `/runs/{id}/events` | `Last-Event-ID` 补读 | SSE |
| POST `/runs/{id}/cancel` | 请求停止后续执行 | 当前运行状态 |
| POST `/runs/{id}/retry` | 失败恢复、独立幂等键 | 202，恢复说明与预计额外消耗 |
| POST `/knowledge-bases/{id}/documents` | multipart 上传 ≤20 MB | 202，文档状态 |
| GET/DELETE `/documents/{id}` | 查询/删除授权文档 | 状态/204 |
| GET `/documents/{id}/source` | 校验当前用户权限 | 来源预览或有界下载 |
| GET/POST/DELETE `/memories` | 本人偏好与确认 | 列表/201/204 |
| GET `/usage` | 本人用量，管理者可查空间 | Token 与预估费用 |
| GET `/admin/audit` | 受限管理员 | 脱敏审计列表 |

## SSE

事件类型 `text_delta`、`tool_status`、`checkpoint_restored`、`completed`、`error`；载荷包含 `run_id`、`event_id`、`sequence`、`payload`。序号单调递增，客户端按事件 ID 去重。每次运行尝试只有一次终态结算；用户确认重试后同一运行有新的事件区间，旧终态不会终止新的消费。`checkpoint_restored.payload.text` 替换未提交的输出片段，不是追加文本。

事件日志首版保留 24 小时并有条数上限；游标过期返回 410，客户端转为查询最终运行详情。断线不取消任务，用户显式取消才触发取消逻辑。

## 七步 RAG 与澄清恢复（已实现）

`GET /runs/{id}` 增加 `state_version`、简短 `rag_trace`，等待补充时返回 `clarification`（问题、选项与过期时间）。`tool_status` 的 `rag_*` 阶段只传公开流程与计数；`clarification` 事件结束本次 SSE 消费切片，任务进入 `waiting_input`，并非执行成功或失败。

`POST /api/v1/runs/{id}/clarify`：请求头 `Idempotency-Key`；请求体 `{ "answer": "补充信息", "state_version": 任务当前版本 }`。只能由任务本人、有效空间成员提交；补充文本最多 1000 字符。成功继续同一 Run，返回当前运行资料，不能另建任务绕过原预算。同键同内容重复返回已有结果；同键不同内容、状态或来源失效返回 409，过期返回 410，预算不足返回 429。可继续调用现有 cancel 接口取消等待。

来源包含 `evidence_id`、`embedding_version`、`collection_name` 和索引版本；合并时附 `parent_id` 及子块身份。`GET /api/v1/sources/{chunk_id}` 可附 `index_version`、`parent_id`、`evidence_id` 核验原文。资料删除、索引版本变化或伪造证据身份返回 404。详情及边界见 [七步增强 RAG](./ENHANCED_RAG.md)。

## 健康接口

`GET /health/live` 仅确认进程存活；`GET /health/ready` 检查必需存储和队列，不触发付费模型调用。业务错误包含 request_id，健康响应不暴露密码或内部连接串。

文档 `progress` 包含 `stage`、`total_chunks`、`embedded_chunks`、`indexed_chunks`、`batch_number`；失败时增加 `failed_stage`。编码与写入分别按已成功批次计数，页面每 2 秒读取文档列表；不把阶段数量当作伪造的总耗时百分比。历史任务可能返回空进度，重试后记录新版本流程。数据库迁移 `0006_document_progress` 为已有文档补空对象，不改变向量索引。


### 删除助手（2026-10-18）

`DELETE /api/v1/agents/{agent_id}`：登录用户携带 CSRF 删除本人助手，成功返回 204。软删除并写入审计；已删除、不存在或其他用户的助手返回 404。删除后助手列表隐藏、禁止发布新版本和创建新任务，已有版本引用、会话及已创建任务保留。需迁移 `0018_agent_deletion`。

# ADR 0003：单 Agent 的执行、事件与模型边界

日期：2026-10-10。状态：实现；真实供应商联调待完成。

## 决策

API 在 PostgreSQL 事务内检查身份、成员权限、本人会话、并发和预算，追加用户消息并创建 queued Run。Celery 队列只携带 Run ID；Worker 从数据库读取不可变版本快照与最近 40 条消息，通过 LangGraph 的模型和工具节点执行有界循环。

AgentVersion 保存提示词、工具白名单、模型标识与限额；不保存供应商密钥或接口凭证。当前部署的模型标识改变后，旧版本不再创建新任务，需要重新发布版本。

每次开始模型请求、退避重试和工具调用前检查取消、权限与截止时间。工具只提供严格 Schema 的文本统计和报告排版，不开放 Shell、文件、网络或任意函数执行。参数错误返回结构化工具结果供模型修正；重复调用、步数和工具数均有上限。

空间、用户父行锁串行保护预算首次创建和运行预留。终态与预算释放在同一事务完成，重复投递与重复终结不会增加消息或重复结算。预算为累计保守额度，按输入 UTF-8 字节数、工具开销、输出上限与最大重试数计入；并非真实 Token 账单。当前不自动按日重置。供应商实际输入／输出 Token 单独记录；用量缺失、Mock 或失败导致不完整时显示未知。没有版本化价格表时费用为空。

每个运行的公开事件单独持久化，客户端只接收文本、工具状态和终态，不直接导出 LangGraph 内部状态或模型推理。SSE 游标采用 `RunID:sequence`，24 小时有效，最多 1000 条事件且预留终态位置。鉴权和权限在每次补读时复查。断网仅影响读取，不取消执行。

## 协议依据

- [OpenAI Chat Completions 官方 API](https://developers.openai.com/api/reference/resources/chat)：文本与工具调用分片，`stream_options.include_usage` 的独立用量块，结束标记；流中断后不宣称完整结果。
- [Anthropic 官方流式协议](https://platform.claude.com/docs/en/build-with-claude/streaming)：message_start、content_block_delta、message_delta 与 message_stop；工具参数按 JSON 增量拼接，用量包含缓存输入。
- [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)：StateGraph、条件边、编译执行与递归上限。

两个 HTTP 协议适配器有脱敏契约测试。认证失败不重试；429、指定服务端错误和网络故障有限退避；已发出文本后不重新请求，避免回复和费用重复。HTTP 客户端不继承系统代理、不跟随重定向，正式端点要求 HTTPS。

## 当前限制与后续

任务创建与队列投递之间仍存在故障窗口；Worker 宕机后的租约接管、事务 outbox、执行检查点和自动恢复在阶段 5 实现。当前重复消费只保证已有 running／terminal 任务不再次启动，不能声称宕机恢复。

创建任务的幂等键作用域为用户，参数必须一致，保存至对应会话被删除；删除会话后不可继续重试原创建请求。SSE 重连只重读事件，不创建新任务。

严格 deadline 检查与 HTTP 读超时限制执行时长，但取消不保证立即中断供应商已收到的请求。取消后不再发起后续调用；已发起调用保守结算。硬中断或进程故障后的悬挂预算恢复也属于阶段 5。

真实供应商尚未配置，不把协议模拟、Mock 或 Docker 健康检查当成真实模型联调。阶段 6 继续处理生产网络、可信代理、扫描、性能与运维。

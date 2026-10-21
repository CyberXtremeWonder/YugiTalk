# 部分证据与流式事件：精读路线

本次改动解决两个实际问题：相关资料被评判为部分支持后清空，以及细碎文字片段消耗任务事件上限。

## 1. 从评判契约开始

阅读 `backend/app/rag/contracts.py` 的 `EvidenceGrade` 与 `safe_route`。

- `answer`：强相关、充分、无歧义。
- `partial_answer`：强相关、部分支持、无歧义。
- `rewrite`：需要改写查询再尝试；最多一次。
- `clarify`：问题缺少实质性条件，需要用户补充。
- `no_knowledge`：没有可用于回答的有效证据。

`partial_answer` 不允许弱相关资料进入回答。`backend/app/rag/model.py` 将相同契约交给评分模型。

## 2. 看程序如何纠正评分器建议

阅读 `backend/app/rag/pipeline.py` 的 `grade` 和 `synthesis` 分支。

模型评分是建议，程序路由是最终决定。旧评分器仍可能返回“强相关、部分支持、rewrite”；程序将其转成 `partial_answer`，避免再次扩题或清空来源。

合成阶段同时保留 `answer` 和 `partial_answer` 的证据；其他终止路径清空证据。原始 `grade` 保留评分器输出，`route` 和执行轨迹记录实际采用的路由，排查时要区分二者。

## 3. 跟踪覆盖状态怎样传到回答模型

新任务在提交时复用 `local_plan` 判断短单意图问题，并冻结 `direct_mode`。
简单问题由 `run_direct` 直接检索并流式回答，省去主助手委派和二次汇总；复杂任务、
已选 Skill 和闭环任务仍走 `run_router`。旧任务没有该标记时保留原路径，恢复时不重新路由。
两条路径均使用同一套工具权限、来源撤销校验与检查点；直接执行也不会重复执行已完成工具。

执行助手首轮收到 `knowledge_catalog` 数据消息，仅含当前用户前 20 个库的 ID、名称及
`next_offset`，可直接选择库检索；更多目录使用 `list_knowledge_bases` 分页。
目录保持用户级数据优先级，不授予额外权限；恢复沿用已保存消息，不重复注入目录。

阅读 `backend/app/agents/subagents.py`：

1. `search` 返回 `coverage` 和真实 `sources`。
2. `delegate` 将检索状态与来源一起传回主助手，避免只留下自然语言总结。
3. `ROUTER_PROMPT` 和 `WORKER_PROMPT` 要求保留原问题范围。

显式选择知识库的路径在 `backend/app/tasks/execution.py` 中，把同样的信息放入内部 `rag_evidence` 上下文。

外层检索不设置次数额度，跨库、改换关键词和同轮批量提案都可以继续执行。检索和工具调用统计次数与 Token 用量，Token 预算开关启用时按预算校验；不再通过 `stop_search` 撤去检索工具，旧检查点的该标记也不参与授权。

同轮连续的 `knowledge_search` 提案经 `tool_batch_executor` 受控并行，沿用任务冻结的
`rag_parallel_queries`（默认 4）控制同时执行的完整检索流程，不限制总检索次数。
其他工具及委派保持原顺序。异步协程共享事件循环，数据库回调使用独立短事务串行执行；
返回消息仍按原提案顺序配对，已完成结果逐项保存。任一致命错误会取消并等待其余协程退出，
恢复时复用已经保存的结果，未完成流程沿各自 RAG 检查点继续。

同任务、同知识库、相同查询（忽略首尾空白）复用检索与评分结果；同批相同查询也只实际执行一次。
缓存保存在任务检查点的工具记录中，不跨任务或跨用户共享。命中前复核当前授权、来源版本，
知识库可用文档集合或索引版本变化后失效；空结果同样受版本校验，不能遮蔽后来新增的资料。
复用在 Trace 中标记 `reused`，不产生新的检索/评分模型请求，也不伪造 Token 消耗。
结果提供 `new_evidence_count` 和已有查询信息，提示模型仅针对明确缺口补查；这是回答指导，
不会因为证据条数或检索次数撤去工具权限。

然后阅读 `backend/app/agents/answer_policy.py`：

- `evidence_policy` 合并证据包，某个空库不能删除其他库的命中。
- `answer_instruction` 分别约束充分支持、部分支持和无证据回答。
- `validate_references` 校验回答中的证据 ID 是否属于实际证据包；来源由卡片展示，模型自行生成的 `[S1]` 编号会被拒绝。

最后看 `backend/app/agents/graph.py` 的 `model`：每轮生成前重算约束，充分支持时不超过 2048 Token，部分支持、无证据或预算收尾时不超过 1024 Token，同时尊重用户配置的更小上限。内部元数据会在调用模型前移除。规划和检查调用继续遵守自己的 JSON 契约。

工具调用不设置次数额度，历史 `max_tool_calls` 字段仅保留兼容。普通执行的最后一轮模型调用保留给回答，模型轮数和超时限制继续生效。真实权限撤销等错误仍立即终止。

阅读 `backend/app/providers/client.py`：DeepSeek Flash 的有界回答使用与结构化 RAG 相同的非思考协议，把本次预算用于可见正文；普通调用保留原行为。模型轨迹记录 `max_output_tokens` 和 `bounded_answer`，方便区分 Agent 配置与实际请求上限。

注意：引用校验在本轮正文生成结束后执行；已经流式显示的内容可能仍出现在失败任务的部分正文里，不能把这类失败回复视为已通过引用校验。

## 4. 看文字怎样变成事件

阅读 `backend/app/agents/streaming.py` 的 `TextStream`，再回到生成图的 `publish` 和 `finally`：

- 累积达到 128 字符，或新片段到达时距上次发布超过 150ms，发布一批。
- 模型正常结束、超时或截断时，通过 `finally` 交付尚未发布的尾部。
- 连续三次重复长度为 80–800 字符的相同尾部片段，报 `model_repetition`。
- 检测窗口只保留 2400 字符，避免检测成本随整篇正文增长。

150ms 是片段到达时检查的阈值，没有额外后台计时器。重复保护是有界启发式检查，刻意输出重复长段落也可能触发。

## 5. 区分事件上限、截断与续传

阅读 `backend/app/core/config.py` 的 `run_event_limit`，以及 `backend/app/tasks/execution.py` 的 `update`、`finish_run`。

默认仍为每任务 1000 条持久事件。文字事件先合并，终态仍预留位置。上限限制事件数，不限制字数或 Token；极慢且细碎的长输出仍可能触顶。截断错误会公开 `truncated=true`。

阅读 `backend/app/tasks/router.py` 的 `events`：

- `types` 允许选择事件类型。
- 终态、澄清和检查点恢复事件不能被过滤。
- 被过滤的事件仍推进原始序号；批尾必要时发一个 `cursor` 帧。

阅读 `frontend/src/tasks/api.ts` 的 `readEvents`：页面不消费 `cursor`，但会保存它的序号；重连通过 `Last-Event-ID` 从已处理位置继续。按序号去重可以防止网络重放造成重复拼接。

断线续传只补读已存在的事件，不自动重新运行触顶或重复保护终止的模型调用。

## 6. 用测试反向理解边界

阅读 `backend/tests/test_answer_boundaries.py`：部分证据保留、跨库空结果、伪造引用、生成预算、单字流合并、异常尾部、重复检测、慢流发布、过滤订阅、触顶错误和续传都有独立场景。

阅读 `frontend/src/tasks/api.test.ts`：重点看过滤游标怎样在不进入页面回调的情况下参与断线续传。

阅读 `backend/tests/test_tool_budget_finish.py`：检查跨额度批量提案、父子共享额度、检索次数限制、权限错误传播和 DeepSeek 请求预算。

阅读 `frontend/src/chat/ChatView.tsx` 的 `recordStep` 和 `ProcessSummary.tsx`：同一任务的相同阶段只保留第一次出现，完整多轮记录仍保留在后端；`process-render.test.tsx` 验证非相邻重复阶段也只展示一次。

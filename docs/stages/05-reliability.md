# 阶段 5：可靠任务、恢复与幂等

日期：2026-10-10。阶段 0–5 已获用户确认；阶段 5 人工验收通过，阶段 6 生产完善开始开发。

## 交付范围

既有功能的目录重构与中文说明补充见 [模块归档验收](./05-modules.md)，最新后端回归为 119 项通过、3 项跳过；新增用例验证独立进程的完整实体注册。

- PostgreSQL 持久排队意图、模型/工具边界检查点及工具游标；Redis 暂不可用时由 Scheduler 补投。
- Worker 租约、阻塞期间续租、旧令牌写入拦截与并发领取保护。
- 文本统计及报告排版只读工具，已提交结果通过执行账本复用。
- 任务记录页确认恢复可重试模型故障，重新检查权限、预算、并发与次数；提示可能增加模型费用。
- 文档宕机后回收租约，以新索引版本从原文重建；删除任务可重复清理。
- 中文注释、迁移 0007_reliability、统一启动文件、Compose Scheduler 服务及缓存收尾脚本。

**范围调整：按用户要求，创建工单及配套授权、审批卡、审批 API 和工单列表已移除，待全部阶段完成后再拓展。** 历史版本与数据库工单数据保留。旧助手配置在使用时过滤 create_ticket；旧待审批任务以 tool_removed 结束并释放预算。当前接口不提供工单或业务审批。

## 运行与配置

```powershell
docker compose up --build -d --wait --wait-timeout 180
docker compose ps
```

API、Worker、迁移与 Scheduler 复用 yugitalk-backend:0.1.0。本机 Scheduler 使用 `backend/.venv/Scripts/python.exe backend/start.py --mode scheduler`；Worker 在 Linux 容器运行。Windows/Linux 本地模型及服务器兼容接口仍可独立配置。

| 配置 | 默认 | 作用 |
| --- | --- | --- |
| RUN_LEASE_SECONDS | 45 | 租约有效期，阻塞期间每三分之一周期续租 |
| RECOVERY_INTERVAL_SECONDS | 5 | Scheduler 扫描间隔 |
| RUN_MAX_ATTEMPTS | 5 | 故障恢复和用户重试领取次数上限 |

自动补投同一任务至少间隔 10 秒，单次最多扫描 200 条到期记录。恢复还依赖调度器、队列、Worker 和数据库可用。

## 自动验收

```powershell
pwsh -NoProfile -File scripts/test-stage5.ps1 -IncludeContainers -IncludeUI
```

本次范围调整及目录重构已通过自动验收：Ruff、格式、mypy、ESLint、TypeScript 和构建通过；后端 119 项通过、3 项跳过（阶段 0 独立容器测试），前端单元测试 22 项通过，阶段 2–5 浏览器测试 22 项通过。后端覆盖下线工具及接口、旧版本不变性、历史数据保留、旧暂停任务预算释放、工具结果提交后宕机复用、租约栅栏与增量结算，以及独立进程的实体注册。容器测试验证真实 SIGKILL 与 8 个并发领取，仅产生一个工具执行记录和一条答复，没有创建工单，任务预算全部释放。

浏览器验证只读报告、工单入口移除、任务记录和手机恢复按钮及无障碍检查；恢复按钮的费用及请求契约使用明确标注的 UI 接口替身，真实恢复与并发另由后端和容器测试验证。最新前端镜像与隔离验收镜像一致。真实本地 BGE-M3 固定中文检索评测通过，Top-1 为 90%、Recall@5 为 100%；临时数据库、容器、对象桶与集合已清理。

当前截图：[任务记录](./artifacts/stage5-tasks.png)、[手机恢复入口](./artifacts/stage5-retry-mobile.png)。

证据：[后端](./artifacts/stage5-backend-tests.xml)、[前端](./artifacts/stage5-frontend-tests.xml)、[浏览器](./artifacts/stage5-ui-results.json)、[真实宕机与账本核对](./artifacts/stage5-recovery.json)、[隔离资源清理](./artifacts/stage5-integration.json)、[中文检索评测](./artifacts/stage5-rag-evaluation.json)、[缓存清理](./artifacts/stage5-cache-cleanup.json)。旧审批截图为先前范围的历史产物，不用于当前验收。

## 人工验收

1. 刷新工作台，进入 Agent 页；仅有文本统计与报告排版，没有创建工单选项。
2. 发布或选择现有助手，新建对话并输入 `请使用 format_report 将“任务恢复验证”整理为 Markdown 报告`；完成后应显示公开工具流程和回答。
3. 打开任务记录并刷新，正常显示本人任务，没有工单列表或操作审批卡。
4. 对可恢复的模型错误，检查“确认恢复任务”和额外计费提示；点击后打开对应会话继续恢复。
5. 执行完整验收脚本，查看真实 Worker 宕机、重复投递、工具账本及预算核对报告。仅操作隔离测试容器，无需终止开发服务。
6. 真实 DeepSeek 的工具提案仍依赖模型能力、提示词及工具授权；普通回答不能充当工具调用验收。

## 边界与缓存

已提交的只读工具结果不会重放；模型未提交响应可能重新生成并增加费用。前端只展示公开流程，不显示模型内部推理。外部写工具的审批、幂等与对账另在项目完成后验收，生产发布仍待阶段 6–7。

本次移除不新增依赖，不删除历史工单数据、模型缓存、虚拟环境或数据卷。依赖下载使用项目 .cache；每轮验收结束按 uv.lock 定向清理 C 盘依赖缓存。本轮缓存检查与清理已完成，无新增项目缓存可释放；旧镜像已按项目标签和容器引用定向检查。当前十个常驻服务均健康，开发数据库的历史工单保留，迁移版本仍为 0007_reliability。

设计取舍见 [ADR 0005](../adr/0005-reliable-execution.md)。

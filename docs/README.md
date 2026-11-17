# YugiTalk 文档导航

更新日期：2026 年 11 月 13 日。

当前使用指南描述现有源码；阶段报告、ADR、评测与归档保存历史事实，不能将某次检查结果当作当前运行状态。每个主题维护一个主要入口，其他文档通过链接引用。

## 阅读入口

| 目的 | 文档 |
| --- | --- |
| 安装与运行 | [项目 README](../README.md)、[开发环境](DEVELOPMENT.md) |
| 理解实现 | [架构](ARCHITECTURE.md)、[项目流程](PROJECT_FLOW.md)、[模块目录](MODULE_GUIDE.md) |
| 查接口与数据 | [API](API.md)、[数据库结构](DATABASE_SCHEMA.md)、[用户归属](USER_OWNERSHIP.md) |
| 理解人工介入 | [HITL 流程与代码](HITL.md)、[规划检查闭环](AGENT_CLOSED_LOOP.md) |
| 测试与评测 | [测试计划](TEST_PLAN.md)、[评测目录](evaluation/README.md) |
| 需求与历史决策 | [需求说明](REQUIREMENTS.md)、[ADR](adr/)、[阶段报告](stages/) |

## 运行与维护

- 开发与发布：[开发](DEVELOPMENT.md)、[生产部署](DEPLOYMENT.md)、[首次生产部署](FIRST_DEPLOYMENT.md)、[运行排障](RUNBOOK.md)。
- 数据与模型：[持久数据目录](PROJECT_DATA.md)、[数据生命周期](DATA_LIFECYCLE.md)、[S3](S3_SETUP.md)、[GPU 嵌入](LOCAL_GPU_EMBEDDING.md)、[Ollama](OLLAMA.md)。
- 入口与观测：[腾讯云网关](TENCENT_GATEWAY.md)、[监控与流式事件](OBSERVABILITY.md)、[并发与 SSE 通知](CONCURRENCY.md)、[邮件告警](ALERTING.md)、[Langfuse](LANGFUSE.md)。

## 功能实现

| 主题 | 入口 |
| --- | --- |
| 知识与 RAG | [增强 RAG](ENHANCED_RAG.md)、[分块与导入排障](DOCUMENT_CHUNKING.md)、[知识图谱](KNOWLEDGE_GRAPH.md)、[提示词](RAG_PROMPTS.md) |
| 工具与扩展 | [工具管理](TOOL_MANAGEMENT.md)、[MCP](MCP.md)、[Skill](CUSTOM_SKILLS.md)、[种子下载](TORRENT_TOOL.md) |
| 对话与文件 | [聊天附件](CHAT_UPLOADS.md)、[研究和文件交付](DEEP_RESEARCH.md)、[旧目录读取兼容](FILE_WORKSPACE.md) |
| 记忆与安全 | [记忆排序](MEMORY_RANKING.md)、[Prompt 注入防护](PROMPT_INJECTION_DEFENSE.md)、[注册与工具审核](REGISTRATION_AND_TOOL_REVIEW.md) |
| 评测方法 | [检索评测](evaluation/RETRIEVAL_EVALUATION.md)、[RAG 调优](evaluation/RAG_TUNING.md)、[LangSmith 适配](RAG_EVALUATION.md) |

## 界面与交互

[UI 规范](UI_DESIGN.md) 维护公共视觉和布局；专题说明具体操作：[Agent](AGENT_WORKSPACE_UI.md)、[Skill MCP Tool](CONFIGURATION_WORKSPACES_UI.md)、[知识库](KNOWLEDGE_LIBRARY_UI.md)、[任务与对话菜单](TASK_HISTORY_UI.md)。

`archive/ui/` 是阶段 0 的静态草图，不能发送真实任务；实际应用入口为 `frontend/src/main.tsx`。

## 历史资料与整理记录

- `stages/` 保存阶段交付和验收，旧接口与测试数量只代表当时版本。
- `stages/artifacts/` 保存运行报告和截图，通常不随 Git 分发；不同运行位置的证据不因内容相同而自动删除。
- `evaluation/` 保留数据集、候选池和资源条件，检索命中率不等于答案准确率。
- `adr/` 保留原设计决策；`archive/` 保存开发复盘、安全扫描快照及静态草图。

| 原位置或重复文档 | 当前入口 |
| --- | --- |
| `PROJECT_MANUAL.md` | 本导航及架构、开发、API 专题 |
| `START_AND_MONITOR.md`、`DEMO.md` | 开发启动见 DEVELOPMENT，环境区分、监控和演示检查见 RUNBOOK |
| `DOCUMENT_INDEX_TIMEOUT.md`、`PDF_IMPORT_NUL_FIX.md` | DOCUMENT_CHUNKING 的导入排障章节 |
| `HERMES_UI.md` | UI_DESIGN 的当前蓝白工作台规范 |
| `WORKSPACE_DELETION.md` | 共享工作空间已移除，现行权限与迁移见 USER_OWNERSHIP |
| `RAG_STREAMING_READING.md` | 证据约束并入 ENHANCED_RAG，事件合并和续传并入 OBSERVABILITY |
| `exports/` 的旧流程 Word | 删除过时导出，维护 PROJECT_FLOW 源文档 |
| 根目录 `AGENT_REQUIREMENTS.md` | `docs/REQUIREMENTS.md`，保留原需求并标明历史边界 |
| 根目录阶段六测试 Compose | `deploy/tests/`，调用脚本已同步 |

整理前副本保存在本机 `.cache/docs-cleanup-20261113-before.tar.gz`，变更清单在同目录的 `docs-cleanup-20261113.json`；这些本机材料不随仓库分发。

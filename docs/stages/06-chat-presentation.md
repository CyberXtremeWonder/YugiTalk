# 对话显示与额度调整验收

日期：2026-10-11。本轮更新本地开发环境 `http://127.0.0.1:8080/`；不代表阶段 6 正式上线门槛通过。

## 本轮行为

- 知识库回答正文隐藏 `[S1]`、`[S2][S3]` 等来源索引。模型提示词不再要求输出索引，服务端保存终态回复时再次清理，覆盖成功、失败和取消。
- 生成中的正文隐藏已完成及半截索引；旧历史消息在显示时清理，不批量改写既有数据库记录。普通用户输入和未选择知识库的对话不作此处理。
- 来源卡片通过文件名、页码或段落打开原文；内部 `label`、`evidence_id` 和索引版本仍保留，不改变来源权限和删除校验。
- 开发与生产模板默认 `TASK_BUDGET_ENABLED=false`、`CUMULATIVE_BUDGET_ENABLED=false`。默认不按单次预留或累计 Token 用量拒绝对话，继续记账；页面改为“估算用量”。既有自定义部署若显式设置为 true，仍按其配置拦截，需改为 false 后重新创建应用容器。
- 并发、超时、上下文容量、模型输出上限、工具和步骤上限继续生效。供应商自身服务额度由供应商控制。
- 已完成的固定系统策略、低优先级 Skill 数据、工具许可检查、安全 Markdown 和 CSP 防注入措施继续保留。

## 自动验证

- 后端全量回归：238 通过、3 跳过，见 `artifacts/chat-unlimited-backend-tests.xml`。新增的三种终态正文清理测试随后单独验证：3 通过，见 `artifacts/chat-terminal-tests.xml`。
- 前端单元测试：34 通过，含跨增量索引、相邻索引和普通 Markdown 保留；Lint、类型检查和构建通过。
- Ruff、MyPy 通过；Python 679 个声明与前端 68 个声明均有中文说明。
- 真实开发镜像 + 只读 API 替身验证历史隐藏、来源预览、用户文本保留及刷新。见 `artifacts/chat-presentation-ui-validation.json` 和 `artifacts/chat-presentation-browser.png`。
- 真实隔离 FastAPI + 内存 SQLite + Mock 的自定义 Skill 与安全 Markdown 浏览器回归通过。未提交用户任务、修改用户业务数据或请求付费模型。
- API、Worker、Scheduler、Frontend 容器已更新，迁移容器刷新；无新增数据库迁移。运行时两个额度开关都为 false，空间/用户并发为 4/2，任务超时为 300 秒。前后端健康检查均为 200。
- 开发 Compose 验证通过；生产模板使用显式占位值做语法验证通过。真实 `.env.production` 缺正式配置，未部署生产栈或连接外部 S3。
- 项目 C 盘依赖缓存检查完成，本轮可清理项为 0 MiB。Docker 项目旧镜像检查完成；保留仍被其他容器引用的镜像，不删除数据或 BGE 权重。

## 人工验收

1. 刷新工作台，重新打开已有知识库对话：正文不出现 `[S2]` 等索引，展开“检索来源”并点击文件名仍可查看原文。
2. 选定知识库发送有证据的问题：生成中及结束后均不显示索引；无相关证据仍回复“没有相关知识”。真实模型请求由你手动验收，本轮自动验证使用离线 Mock。
3. 正常持续对话，不再因项目 Token 额度耗尽返回拒绝；底部只显示用量。原有账本和已失败任务不会被自动删除或重跑。

实现入口：`backend/app/tasks/execution.py`、`backend/app/tasks/access.py`、`backend/app/core/config.py`、`frontend/src/rag/presentation.ts`、`frontend/src/rag/Sources.tsx`、`frontend/src/chat/ChatView.tsx`。注入防护代码讲解见 [安全说明](../PROMPT_INJECTION_DEFENSE.md)。

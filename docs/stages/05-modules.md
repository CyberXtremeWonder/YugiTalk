# 阶段 5 补充：功能模块归档与中文说明验收

日期：2026-10-10。已有功能完成目录重构和自动回归；阶段 5 仍等待用户人工验收，生产阶段尚未开始。

## 交付内容

- 后端按身份、空间、助手、对话、模型适配、工具、任务、RAG、记忆、审计、健康和公共设施归档。接口和实体均进入对应目录，移除已空的 `api/`、`services/`、`workers/`。
- 前端按功能保存组件、类型、API 与单元测试；任务事件消费从助手协议中独立出来。
- RAG 与离线 BGE-M3 推理代码统一位于 `backend/app/rag/`；模型 Dockerfile、构建上下文和排除规则已同步。
- Python 全部自有类和具名函数、前端全部自有类、方法和具名函数补齐中文说明。增加完整性检查并接入阶段 5 与 CI。
- 统一数据库初始化入口注册完整实体，独立 Worker 和 Scheduler 无需加载 API 就能解析外键。新增独立子进程回归，防止目录拆分后任务长期停留在排队状态。
- README、需求文档、项目说明书和 [模块目录说明](../MODULE_GUIDE.md) 同步更新。

## 自动验收

| 检查 | 最终结果 |
| --- | --- |
| 后端回归 | 119 通过，3 跳过；跳过项为阶段 0 独立容器测试 |
| 前端单元测试 | 22 通过 |
| 阶段 2–5 浏览器回归 | 22 通过 |
| 中文说明覆盖 | Python 392 个声明、前端 57 个声明，缺失项为 0 |
| Ruff、格式、mypy、ESLint、TypeScript、构建与配置预检 | 通过 |
| 真实队列和 Milvus 读写 | 通过 |
| 真实任务恢复 | SIGKILL 后恢复成功；8 次并发重复投递没有重复工具执行或重复答复 |
| 本地 BGE-M3 固定中文检索评测 | Top-1 90%，Recall@5 100% |
| 开发部署 | 10 个常驻服务健康，迁移服务成功退出 |
| 验收资源清理 | 随机测试数据库、容器、对象桶与集合已清理 |
| 缓存与镜像收尾 | 定向检查项目旧镜像和 C 盘依赖缓存，本轮无新增可释放缓存 |

证据：[后端回归](./artifacts/modules-backend-tests.xml)、[前端单测](./artifacts/stage5-frontend-tests.xml)、[浏览器](./artifacts/stage5-ui-results.json)、[真实恢复](./artifacts/stage5-recovery.json)、[隔离集成](./artifacts/stage5-integration.json)、[Python 中文说明](./artifacts/modules-python-comments.json)、[前端中文说明](./artifacts/modules-frontend-comments.json)、[缓存收尾](./artifacts/stage5-cache-cleanup.json)。

## 人工验收

1. 打开工作台，使用已有账号登录；切换空间，检查设置页的成员管理仍可用。
2. 在对话页发送消息，检查简短公开流程、回复、停止生成和历史记录。
3. 在知识库页上传一份 UTF-8 文本，等待可用后检索并查看来源；删除后资料从列表消失。
4. 打开任务记录，检查状态与恢复入口；助手和任务页面均没有工单入口。
5. 按 [模块目录说明](../MODULE_GUIDE.md) 打开各目录，检查类和方法的中文说明。执行以下命令应返回通过：

```powershell
backend/.venv/Scripts/python.exe scripts/check-code-comments.py
npm --prefix frontend run check:comments
```

完整复跑仍使用 `scripts/test-stage5.ps1 -IncludeContainers -IncludeUI`。目录与注释调整不改变现有 API 路径、数据库表和用户数据；旧内部 Python 导入路径已迁移，外部自有脚本需按目录说明调整。

用户追加的七步 RAG 编排已单列在需求文档的 4.6.1，属于后续待实现、待验收的扩展。本轮通过的是模块归档、中文说明和既有功能回归，不能代表原生 BM25、RRF、Auto-merging、Grader 或 RAG HITL 已实现。

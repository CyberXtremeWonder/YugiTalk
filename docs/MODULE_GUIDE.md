# 功能模块目录说明

更新日期：2026-10-11。已有实现按功能归档；自定义技能新增独立 `skills/` 模块与迁移。

## 后端

| 目录（位于 `backend/app/`） | 职责 | 主要入口 |
| --- | --- | --- |
| `identity/` | 登录、会话、密码、来源与 CSRF 校验、用户初始化 | `router.py`、`dependencies.py`、`bootstrap.py` |
| `accounts/` | 用户资源直接归属与会话范围 | `access.py` |
| `agents/` | 助手配置、版本发布、执行图 | `router.py`、`contracts.py`、`graph.py` |
| `skills/` | 空间自定义流程、版本、内容校验和工具收窄 | `router.py`、`service.py`、`models.py` |
| `guardrails/` | 固定安全策略、上下文降权、旧消息恢复和工具授权复核 | `prompts.py`、`tools.py` |
| `chat/` | 私有对话、消息历史、对话所有权 | `router.py`、`access.py`、`models.py` |
| `providers/` | Mock、OpenAI 兼容、Anthropic 模型协议及流式响应 | `client.py` |
| `tools/` | 只读工具参数契约、固定注册表与执行 | `contracts.py`、`registry.py`、`runner.py` |
| `tasks/` | 任务、SSE、预算、取消、检查点、幂等、Worker 与 Scheduler | `router.py`、`execution.py`、`reliability.py`、`worker.py` |
| `rag/` | 知识库、资料、解析分块、嵌入、向量检索与来源 | `router.py`、`indexing.py`、`parser.py`、`embeddings.py` |
| `memory/` | 用户确认的长期记忆、偏好开关与上下文摘要 | `router.py`、`context.py`、`models.py` |
| `audit/` | 审计记录、事务内写入与管理员查询 | `router.py`、`service.py`、`models.py` |
| `health/` | 存活、就绪、依赖探测 | `router.py`、`checks.py`、`milvus.py` |
| `core/` | 共享配置、连接、日志、分页与实体注册 | `config.py`、`database.py`、`entities.py`、`model_registry.py` |

`main.py` 组装应用和路由；`backend/start.py` 保持统一启动入口。业务规则放在所属模块内，其他模块导入其契约或服务，避免从路由文件导入业务工具。没有为了填满目录而创建空业务模块。

数据库实体分布在对应模块的 `models.py`，共用 `core/entities.py` 的 `Base`，由 `core/model_registry.py` 注册完整元数据。注册接入共享数据库初始化入口，确保独立启动的 Worker 和 Scheduler 也能解析完整外键。目录整理本身未修改表结构；后续 RAG 层级迁移为 `0008_rag_hierarchy`，自定义 Skill 迁移为 `0009_custom_skills`。

任务执行账本及历史审批、工单映射在 `tasks/ledger_models.py`。历史数据保留，当前没有创建工单或审批业务接口。

## 前端

`frontend/src/identity/`、`accounts/`、`agents/`、`skills/`、`chat/`、`tasks/`、`rag/`、`memory/`、`health/` 分别保存对应功能的组件、类型与 API 适配。`shared/` 保存导航定义和工作台样式；`App.tsx`、`main.tsx` 负责应用组装。

对话消息类型在 `chat/api.ts`，任务类型和 SSE 消费在 `tasks/api.ts`，用户归属类型在 `accounts/api.ts`，助手类型在 `agents/api.ts`。认证请求工具在 `identity/api.ts`，统一同源 Cookie 和写入防伪校验。

单元测试与相关功能同目录。浏览器测试仍按阶段保存在 `frontend/tests/stageN/`，方便按已验收阶段回归；后端测试集中在 `backend/tests/`。

## 本地模型与部署

本地 BGE-M3 推理实现在 `backend/app/rag/local_embedding.py`。独立模型镜像从此文件构建，`embedding/` 保存 Dockerfile 和模型依赖清单；API 和 Worker 复用模型服务。

模型镜像的构建上下文改为项目根目录。根目录 `.dockerignore` 只放行此镜像所需的源码和依赖清单，排除 `.env`、缓存、模型权重及虚拟环境。模型通过只读卷加载本地缓存；服务器模式继续使用兼容嵌入接口。

本机与容器入口保持可用：

```powershell
cd backend
.venv/Scripts/python.exe start.py --check
.venv/Scripts/python.exe start.py --reload
```

```powershell
# 项目根目录
docker compose up --build -d --wait --wait-timeout 180
docker compose exec api python -m app.identity.bootstrap --email you@example.com --name "你的名字"
```

外部脚本若导入旧 `app.services`、`app.api`、`app.workers` 或集中模型文件，需要按上表更新路径。前后端对外接口未改名。

## 中文说明与维护

Python 每个类和具名函数使用中文 docstring；前端每个类、方法和具名函数使用中文 JSDoc。匿名回调在权限、并发、超时等复杂边界处保留中文行内说明。第三方依赖和生成产物不修改。

```powershell
backend/.venv/Scripts/python.exe scripts/check-code-comments.py
npm --prefix frontend run check:comments
pwsh -NoProfile -File scripts/test-stage5.ps1 -IncludeContainers -IncludeUI
```

检查程序覆盖自有实现、迁移、探测、测试与脚本的具名声明，发现缺少中文说明时返回非零退出码。中文说明检查已接入阶段 5 和 CI；说明准确性仍需代码审查。

七步 RAG 已集中到 `rag/`：`pipeline.py` 编排，`contracts.py` 约束结构，`retrieval.py` 并行混合召回与证据身份，`hierarchy.py` 父子分块，`model.py` 模型评分与重写，`rerank.py` 有界重排与熔断，`resume.py` 本人澄清恢复，`rebuild.py` 旧资料受控升级。`tasks/` 继续负责租约、事件、预算和检查点持久化，避免在 RAG 中复制任务执行器。

辅助脚本：`scripts/rebuild-rag-index.py` 显式升级一份旧资料；`scripts/check-enhanced-rag.py` 只在隔离环境核验真实存储；`scripts/run-enhanced-rag-ui.py` 初始化专用浏览器验收账号并删除测试资料。配置与人工验收见 [七步增强 RAG](./ENHANCED_RAG.md)。新增其他功能沿用对应目录，每次修改同步回归调用方和阶段验收文档。

## 生产观测与操作

`backend/app/observability/` 负责认证指标、有界追踪和元数据保护；`deploy/` 保存Nginx、Prometheus与Jaeger模板；`scripts/init-production.py`、`production-data.py`、`deploy-production.py` 分别负责目录配置、冷备恢复和兼容Schema内镜像发布。操作见 [DEPLOYMENT](./DEPLOYMENT.md)，未通过门槛见 [阶段6](./stages/06-production.md)。

`backend/app/tasks/database.py`维护Worker的进程独占连接池。导入模块不连接数据库，检测进程身份以防fork继承连接，退出信号释放池；每项任务继续使用独立Session，配置修改须重启服务。`scripts/build-production-images.py`在重建镜像前保留上一镜像，`audit-stage6-images.py --candidate-image ...`仅离线扫描候选，不部署或迁移数据。

`backend/app/rag/storage.py`负责S3 SDK、Region、寻址和CA选项，正式环境不自动建桶；`scripts/check-production-s3.py`负责接通及可选小对象读写删除检查；初始化脚本同时生成Milvus S3配置。真实外部恢复尚缺，本机备份/发布会明确拒绝外部S3。[接入说明](./S3_SETUP.md)

## 工具目录与执行

`backend/app/tools/` 集中保存工具管理的 `models.py`、`router.py`、`management_contracts.py`、`service.py`、`default_http.py` 与 `http_runtime.py`；`frontend/src/tools/` 提供管理页和共用授权选项。任务模块负责租约与账本，模型模块负责将冻结的 Schema 适配协议，Skill 不自行执行工具。[配置与安全流程](./TOOL_MANAGEMENT.md)

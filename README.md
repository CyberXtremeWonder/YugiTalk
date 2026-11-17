# YugiTalk

YugiTalk 是支持自定义 Agent、工具、知识库与后台任务的 AI 工作台。前端使用 React、TypeScript 和 Vite，后端使用 Python 3.12、FastAPI、LangGraph、Celery、PostgreSQL、Redis、S3 兼容对象存储和 Milvus。

[文档导航](docs/README.md) · [开发启动](docs/DEVELOPMENT.md) · [架构](docs/ARCHITECTURE.md) · [项目流程](docs/PROJECT_FLOW.md) · [API](docs/API.md) · [排障](docs/RUNBOOK.md)

## 当前能力

| 功能 | 说明 |
| --- | --- |
| 用户与权限 | 资源直接归登录用户；会话、CSRF、账号管理与工具发布审核；SMTP 注册可选 |
| Agent | 独立配置模型和不可变版本，简单任务直接执行，复杂任务使用 Router 委派 |
| 执行控制 | 流式回复、检查点与租约恢复、暂停继续停止、[用户插话和 RAG 澄清](docs/HITL.md) |
| 知识库 | 多知识库、异步文档导入、[父子分块](docs/DOCUMENT_CHUNKING.md)、Dense 与 BM25 混合检索、可选重排及图谱 |
| 扩展工具 | 内置工具、受限 HTTPS 查询、[MCP](docs/MCP.md)、[Skill](docs/CUSTOM_SKILLS.md)，授权与凭证由后端校验 |
| 文件交付 | [聊天附件](docs/CHAT_UPLOADS.md)、研究资料并交付 Word、PDF、Markdown 等[受控文件](docs/DEEP_RESEARCH.md) |
| 记忆与观测 | 用户确认的记忆、会话摘要、[记忆排序](docs/MEMORY_RANKING.md)、用量、Trace 与可选告警 |

普通对话无需手动选择知识库。任务绑定发布时的模型、工具和资源配置；模型密钥在后端加密保存，不回显。修改 `SESSION_SECRET` 会影响会话及助手密钥解密，API 与 Worker 必须使用相同且稳定的配置。

阶段报告保留各轮验证及未完成项。本机服务健康、Mock 测试或有限评测不等于真实模型质量、外部 S3 恢复及完整生产发布验收。生产流程见 [DEPLOYMENT](docs/DEPLOYMENT.md)。

## 快速启动

需要 Docker Linux 容器和 Compose；本地嵌入服务还需要已有 BGE-M3 权重及足够资源。仅运行 Docker 不要求宿主机安装前后端开发依赖。

Windows PowerShell 在项目根目录运行：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# 首次先编辑 .env，填写当前机器的 BGE 缓存或远程嵌入配置。
.\start.ps1
```

`start.ps1` 检查 Docker、构建并等待健康；有已安装且可用的 CUDA 镜像时保留 GPU 配置。不要覆盖已有私有 `.env`。

Linux、macOS 或已配置的 Bash 使用显式 Compose 命令，仓库当前没有 `start.sh`：

```bash
test -f .env || cp .env.example .env
# 编辑 .env；启用本地嵌入时设置模型路径和 local-embedding profile。
mkdir -p data/postgres data/redis data/minio data/etcd data/milvus .cache/models
docker compose config --quiet
docker compose up --build -d --wait --wait-timeout 600
```

| 入口 | 默认地址 |
| --- | --- |
| 工作台 | http://127.0.0.1:8080/ |
| 开发 API 文档 | http://127.0.0.1:18000/docs |
| 就绪检查 | http://127.0.0.1:18000/health/ready |

首次管理员初始化交互输入密码，没有默认账号；已有账号不要重复初始化：

```powershell
docker compose exec api python -m app.identity.bootstrap --email you@example.com --name "管理员" --platform-admin
```

配置默认聊天模型、远程嵌入、GPU、热更新 5173/8000、停止和重建的完整步骤见 [开发指南](docs/DEVELOPMENT.md)。公网网关与本机开发共用服务，不能视为独立生产环境。

## 项目目录

```text
backend/                 API 领域模块 迁移 测试与统一启动入口
frontend/                业务组件 接口和浏览器测试
embedding/               本地嵌入镜像与依赖
docling/                 文档转换服务镜像
deploy/                  部署模板 网关与隔离测试覆盖配置
  tests/                 阶段六测试和稳定性压测 Compose
scripts/                 初始化 构建 部署 验证及维护工具
docs/                    导航 功能指南 架构 评测及历史报告
data/                    本机持久数据与备份 不提交 Git
.cache/                  依赖缓存及可重建验证产物 不提交 Git
```

根目录保留 `README.md`、`start.ps1`、开发和生产 Compose、GPU/网关/工作空间/告警覆盖配置、环境模板及工程配置。历史需求已归入 [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md)，阶段六测试覆盖配置位于 `deploy/tests/`，不混入日常启动入口。

## 常用操作

```powershell
docker compose ps -a
docker compose logs --tail 100 migrate api worker scheduler
docker compose stop
```

若原环境启用了 GPU 或网关，维护时继续使用原来的覆盖文件。`stop` 保留容器和数据；`start` 启动旧容器，不升级镜像；`restart` 不加载新的环境变量。代码变更需要构建并更新对应服务，配置变更需要重建容器。具体命令与恢复边界见 [RUNBOOK](docs/RUNBOOK.md)。

## 文档与验证

- 当前代码：[架构](docs/ARCHITECTURE.md)、[调用流程](docs/PROJECT_FLOW.md)、[模块](docs/MODULE_GUIDE.md)、[数据库](docs/DATABASE_SCHEMA.md)。
- 测试方法：[测试计划](docs/TEST_PLAN.md)、[检索评测](docs/evaluation/README.md)、[HITL 验证范围](docs/HITL.md#14-验证结果和证据边界)。
- 运行维护：[部署](docs/DEPLOYMENT.md)、[排障](docs/RUNBOOK.md)、[数据生命周期](docs/DATA_LIFECYCLE.md)。
- 历史证据：[阶段报告](docs/stages/)、[设计决策](docs/adr/)、[归档](docs/archive/)。

不提交 `.env`、密钥、模型权重、业务数据和本机运行输出。文档整理不重置用户配置、不清理业务资料，也不自动执行迁移或重启部署。

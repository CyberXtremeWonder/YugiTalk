# YugiTalk

YugiTalk 是支持自定义 Agent、工具、知识库与后台任务的 AI 工作台。前端采用 React + TypeScript + Vite，后端采用 Python 3.12 + FastAPI，使用 LangGraph、Celery、PostgreSQL、Redis、MinIO 与 Milvus。

Agent 页面支持独立配置模型提供商（OpenAI 兼容协议、Anthropic、Mock）、模型名称、API 根 URL 和 API Key，也可继续使用服务器默认配置。修改后发布新版本，已有任务保留原版本配置。编辑时 API Key 留空保留原密钥；更换提供商或 URL 时需重新填写。密钥加密保存且不回显，加密依赖 `SESSION_SECRET`，API 与 worker 应使用相同且稳定的值；更换该值后需重新保存各助手的 API Key。非测试环境的模型 URL 须使用 HTTPS。

新对话采用 Router → 执行 Subagent：主助手只负责委派和汇总，执行助手复用所选 Agent 的模型、提示词及工具授权，自主调用业务工具、发现并检索当前用户的知识库。对话窗口无需选择知识库，检索来源仍可查看原文。第一版采用单层串行委派，每轮最多三次；父子执行共用任务预算、取消和租约，检查点分别保存，无需新增数据库表。开启预算限制时，多次模型调用仍计入原任务总额度。旧任务及显式指定知识库的旧客户端保留原执行路径。

当前阶段 0–5 已验收，阶段 6 生产完善仍在进行中，尚未达到完整生产发布条件，详见 [阶段 6 报告](docs/stages/06-production.md)。

## 功能说明

| 功能 | 说明 |
| --- | --- |
| 身份与权限 | 登录、会话校验、用户资源隔离、管理员初始化；配置 SMTP 后支持邮箱验证码注册 |
| Agent | 创建个人助手，选择模型、授权工具并发布版本 |
| 对话与任务 | 流式回复、历史消息、停止生成、任务记录和执行状态 |
| 自定义 Skill | 自定义流程、版本发布，在对话中选择使用并限制可用工具 |
| 模型协议 | Mock、OpenAI 兼容、Anthropic；DeepSeek 使用兼容协议 |
| 工具管理 | 文本统计、报告排版、天气、路况、Tavily 网页搜索及受限的只读 HTTPS JSON 工具；外部服务需配置各自密钥 |
| 知识库与 RAG | 多知识库，TXT、Markdown、文本 PDF 导入，分块与嵌入、混合检索、来源卡片；七步增强 RAG 支持可选重排和澄清恢复 |
| 知识图谱 | 后台抽取有原文依据的关系，最多两跳召回，与分块检索融合 |
| 个人记忆 | 保存、编辑、删除个人偏好，控制记忆开关 |
| 可靠执行 | Worker 租约、图检查点、恢复 Scheduler、只读工具执行账本 |
| 运维 | 健康检查、审计、指标、追踪及生产部署脚本；生产验收仍有未完成项 |

## 项目文件结构

```text
YugiTalk/
├── backend/
│   ├── app/
│   │   ├── main.py          FastAPI 应用与路由组装
│   │   ├── identity/       登录、注册、会话与管理员初始化
│   │   ├── accounts/       用户资源归属校验
│   │   ├── agents/         助手配置、版本与执行图
│   │   ├── skills/         自定义技能与版本
│   │   ├── guardrails/     消息信任与工具授权防护
│   │   ├── chat/           对话与消息
│   │   ├── providers/      模型协议适配
│   │   ├── tools/          工具管理及只读执行
│   │   ├── tasks/          任务、事件流、Worker、Scheduler 与恢复
│   │   ├── rag/            知识库、解析、嵌入、检索与图谱
│   │   ├── memory/         偏好与长期记忆
│   │   ├── audit/          审计
│   │   ├── observability/  指标与追踪
│   │   ├── health/         健康检查
│   │   └── core/           配置、连接与共享基础组件
│   ├── migrations/         Alembic 数据库迁移
│   ├── tests/              后端测试
│   ├── probes/             技术验证
│   ├── start.py            后端启动与配置预检
│   └── pyproject.toml      Python 依赖定义，uv.lock 锁定版本
├── frontend/
│   ├── src/                按业务模块组织的组件、接口及单元测试
│   ├── tests/              Playwright 浏览器测试
│   └── package.json        npm 命令，package-lock.json 锁定版本
├── embedding/              本地嵌入服务镜像与依赖
├── deploy/                 Nginx、监控、网关及历史部署模板
├── scripts/                测试、初始化、迁移、部署与诊断脚本
├── docs/                   架构、功能说明、操作手册与阶段报告
├── .github/workflows/      CI 工作流
├── docker-compose.yml      Docker 开发环境
├── docker-compose.prod.yml 生产环境（需独立配置与验收）
├── docker-compose.gpu.yml  GPU 配置覆盖
├── .env.example            无真实密钥的开发配置模板
├── .env.production.example 无真实密钥的生产配置模板
├── start.ps1 / start.sh    一键启动开发环境
└── data/                   首次运行创建的本机持久数据，不提交 Git
```

详细代码入口见 [模块目录说明](docs/MODULE_GUIDE.md)。运行数据、模型权重、虚拟环境、依赖缓存及运行输出不随仓库分发。

## 环境搭建

### Docker 完整运行（推荐）

准备 Git、Docker Engine 或 Docker Desktop，以及 Docker Compose 插件。Windows 使用 Linux 容器模式。仅用 Docker 时无需在宿主机安装 Python 和 Node.js；首次构建需访问镜像与依赖源。本地 BGE-M3 嵌入服务需预先准备模型权重及足够的可用内存。

克隆本仓库，进入项目根目录，再复制模板。已有 `.env` 时保留原文件：

```powershell
# Windows PowerShell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

```bash
# Linux / macOS / Git Bash
 test -f .env || cp .env.example .env
```

编辑 `.env`，按实际环境配置：

| 配置 | 用途 |
| --- | --- |
| `MODEL_PROVIDER` / `MODEL_ID` | 默认 `mock` / `mock-v1`，聊天演示无需供应商密钥 |
| `MODEL_BASE_URL` / `MODEL_API_KEY` | 真实模型的服务地址及密钥；DeepSeek 等使用 `openai_compatible` |
| `COMPOSE_PROFILES` / `BGE_MODEL_PATH` | 模板启用 `local-embedding`，路径需指向当前机器已有 BGE-M3 缓存；不会自动下载权重 |
| `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` | 远程嵌入服务；使用远程服务时清空 `COMPOSE_PROFILES`，关闭本地嵌入服务 |
| `EMBEDDING_MODEL` / `EMBEDDING_DIMENSION` | 与实际服务一致，模板使用 BGE-M3、1024 维 |
| `SMTP_*` | 邮箱验证码注册；未配置时可通过下方初始化命令创建账号 |
| `TAVILY_API_KEY` / `AMAP_API_KEY` | 可选搜索、天气与路况凭证，同时核对 `TOOL_HTTP_ALLOWED_HOSTS` |
| `AUTH_TRUSTED_ORIGINS` | 实际浏览器来源；自定义端口时同步修改 |

Mock 聊天无需模型密钥，但默认启用的本地嵌入服务仍需要模型文件。模型路径必须按当前操作系统填写。真实密钥只保存在私有配置中，不要放进前端 `VITE_*` 变量。

## 启动项目

在项目根目录运行：

```powershell
# Windows PowerShell
.\start.ps1
```

```bash
# Linux / macOS / Git Bash
bash start.sh
```

脚本检查 Docker、初始化数据目录、校验配置、构建并等待服务健康；迁移容器自动升级数据库。可用 `docker compose config --quiet` 单独校验，避免打印展开后的私有配置。

首次创建管理员，替换为自己的邮箱和名称，密码由终端交互输入：

```powershell
docker compose exec api python -m app.identity.bootstrap --email you@example.com --name "管理员" --platform-admin
```

没有默认登录密码。已有账号直接登录，重复初始化不会覆盖已有密码；创建普通账号可省略 `--platform-admin`。

| 入口 | 默认地址 |
| --- | --- |
| 工作台 | http://127.0.0.1:8080/ |
| API 文档 | http://127.0.0.1:18000/docs |
| 存活与就绪 | http://127.0.0.1:18000/health/live 、 http://127.0.0.1:18000/health/ready |
| MinIO 控制台 | http://127.0.0.1:29001/ |

### 本机热更新开发

额外安装 **Python 3.12**、**uv**、**Node.js ≥ 22.12** 和 npm。数据库、Worker、Scheduler 等依赖继续使用 Docker。Python 要求 `>=3.12,<3.13`。

先配置好 `.env`，以下为项目根目录中的 Windows PowerShell 命令：

```powershell
. ./scripts/set-project-cache.ps1
uv sync --project backend --frozen --python 3.12
npm.cmd --prefix frontend ci
./scripts/init-project-data.ps1
docker compose build api
# 启用本地嵌入时先启动模型；使用远程嵌入时跳过此行
docker compose --profile local-embedding up --build -d --wait --wait-timeout 600 embedding
docker compose up -d --wait --wait-timeout 600 postgres redis storage etcd milvus migrate worker scheduler
```

终端一启动 API：

```powershell
cd backend
.venv/Scripts/python.exe start.py --check
.venv/Scripts/python.exe -m alembic upgrade head
.venv/Scripts/python.exe start.py --host 127.0.0.1 --port 8000 --reload
```

终端二在项目根目录启动前端：

```powershell
npm.cmd --prefix frontend run dev
```

访问 http://127.0.0.1:5173/，Vite 默认将 API 请求代理到 `127.0.0.1:8000`。Linux/macOS 的后端解释器路径为 `backend/.venv/bin/python`，数据目录可通过 Bash 启动流程初始化。更多细节见 [开发手册](docs/DEVELOPMENT.md)。

## 关闭、重启与查看状态

以下命令在根目录执行，默认操作 `yugitalk-dev` 开发环境：

```powershell
docker compose ps -a
docker compose logs --tail 100 api worker scheduler

# 停止全部容器，保留容器与数据
docker compose stop

# 再次启动已有容器
docker compose start

# 停止并移除容器和网络；data/ 绑定目录中的数据仍保留
docker compose down

# 代码更新后重新构建并启动
docker compose up --build -d --wait --wait-timeout 600
```

本机热更新模式下，在 API 与 Vite 终端分别按 **Ctrl+C**，再执行 `docker compose stop`。不要删除 `data/`，不要将 `down -v` 作为日常关闭命令。

修改 `.env` 中的模型、工具密钥后，让执行服务重新读取环境配置：

```powershell
docker compose up -d --no-deps --force-recreate --wait --wait-timeout 90 api worker scheduler
```

本机 API 也需停止后重新启动。生产和阶段验收环境使用独立配置与项目名，见 [启动与监控手册](docs/START_AND_MONITOR.md)。

## 测试与检查

安装本机依赖后可执行：

```powershell
backend/.venv/Scripts/python.exe -m pytest backend/tests
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend test
npm.cmd --prefix frontend run build
```

部分后端测试需要独立数据库或容器，前置条件见 [测试计划](docs/TEST_PLAN.md) 和 [CI](.github/workflows/ci.yml)。阶段入口为 `scripts/test-stage*.ps1`；带 `-IncludeContainers` 的故障演练可能短暂中断开发服务，不要在演示时执行。历史报告中的通过数量只对应当次版本。

## 密钥与提交规则

`.gitignore` 排除私有 `.env` 及变体、数据、运行输出、本机运行时、依赖缓存、测试产物和私钥。只保留清理过的 `.env.example` 与 `.env.production.example`。模板中的开发服务默认值仅用于本机开发，生产必须使用独立强凭证。

不要用 `git add -f` 加入私有文件。提交前检查暂存内容；文档、日志、截图、模板也可能包含凭证。密钥一旦泄露，应在供应商控制台撤销并重新生成。


运行截图、验证产物及大型评测输出默认不上传；历史报告中指向这些产物的链接需在本机运行对应验证后查看。

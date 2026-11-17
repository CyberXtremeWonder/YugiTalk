# 用 YugiTalk 学习本地 Langfuse

第一课的目标：在本机 Langfuse 中看到一条合成父子追踪，再观察真实任务现有的模型、工具、RAG Span。当前阶段只导出已有的元数据，不上传提示词、回答或知识库正文；没有配置模型用量属性、费用或评分。

## 1. 启动独立服务

先启动 Docker Desktop，切换到 Linux containers。在项目根目录执行：

```powershell
docker compose -f deploy/langfuse/compose.yml config --quiet
docker compose -f deploy/langfuse/compose.yml up -d
docker compose -f deploy/langfuse/compose.yml ps
```

访问 <http://localhost:3000>，注册本地账号，创建组织和项目（例如 YugiTalk Learning），在项目设置中创建 API Keys。这里注册的是本机服务的账户。

Web 和后台 Worker 处理追踪，PostgreSQL 保存应用配置，ClickHouse 保存观测数据，Redis 管理队列，MinIO 保存事件与媒体对象。它们有独立网络和命名卷，不复用业务数据库。宿主机仅开放回环地址 3000 和 19090；这是学习环境，内置固定开发凭证，不能作为生产部署。镜像使用主版本标签，首次拉取和后续升级可能得到不同补丁版本。

Langfuse 自身遥测已关闭；自动评分和外部模型功能没有配置。本地部署不改变 YugiTalk 原来模型供应商的网络调用。

## 2. 配置 YugiTalk 的认证

下面两个 Key 使用刚创建的本地项目凭证。在 PowerShell 中生成 JSON（不要粘贴真实密钥到聊天、文档或代码仓库）：

```powershell
$lfPublic = Read-Host 'Langfuse Public Key'
$lfPrivate = Read-Host 'Langfuse Secret Key' -MaskInput
$lfBasic = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("${lfPublic}:${lfPrivate}"))
@{Authorization="Basic $lfBasic"; 'x-langfuse-ingestion-version'='4'} | ConvertTo-Json -Compress
```

把生成的一行 JSON 写入根目录私有 `.env` 的 `YUGITALK_OTLP_HEADERS`，并设置：

```dotenv
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:3000/api/public/otel
YUGITALK_OTLP_HEADERS={"Authorization":"Basic 替换为生成值","x-langfuse-ingestion-version":"4"}
OTEL_SAMPLE_RATIO=1
```

上面地址适用于直接运行在 Windows 上的 Python。若 API/Worker 在 Docker Desktop 的容器内运行，地址改成 `http://host.docker.internal:3000/api/public/otel`。开发 Compose 已转发这三个变量；修改后需重新创建 API/Worker（通常还需重新构建后端镜像），本机 Python 进程需要重启。

注意：`.env` 不会自动成为 PowerShell 环境变量，但后端 Settings 会读取它。导出器会自动追加 `/v1/traces`，不要重复填写。认证头以 SecretStr 存储；Base64 是编码而不是加密。

## 3. 发出第一条追踪

在项目根目录执行（已安装后端依赖）：

```powershell
Push-Location backend
try { ./.venv/Scripts/python.exe -m probes.langfuse_smoke } finally { Pop-Location }
```

在 Langfuse 的 Tracing 页面搜索输出的 trace_id，确认根步骤 `learning.langfuse-smoke` 下存在 `learning.child-step`。程序输出只代表尝试导出，界面查到数据才代表入库成功。

随后提交一条普通 YugiTalk 测试任务，查看 `agent.execute`、`model.generate`、`tool.*` 和 `rag.execute`。实际出现哪些步骤取决于任务是否调用工具或 RAG。数据库中的 `trace_step` 不会自动全部变成 OTEL Span，当前模型步骤也还不是带用量的 Langfuse Generation。

## 4. 学习时观察什么

- Trace 是跨 API、队列和 Worker 关联的执行链；一次 HTTP 请求不一定等于一轮完整会话。
- Span 是链中的步骤；父子关系来自执行上下文。项目通过 Celery 消息的 traceparent 传播上下文。
- Session 用于聚合同一会话的多次任务，需要之后显式接入。
- Generation 表示模型调用；下一课再把模型名、真实用量和流式结束状态映射进去。
- Score 表示质量评价；等追踪稳定后，再固定问题集评估检索证据和引用正确性。

采样设为 1 方便学习；现有默认值为 0.1。对已有父上下文仍遵循父级采样决策。服务不可达时批量导出会报错，不能据此断言业务任务失败。遇到空页面先查地址、认证、采样和两个 Langfuse 服务的日志。

## 5. 停止与排查

```powershell
docker compose -f deploy/langfuse/compose.yml logs --tail 80 web worker
docker compose -f deploy/langfuse/compose.yml stop
```

停止不删除命名卷。关闭 YugiTalk 导出则清空 OTLP endpoint 并重启相关进程。

官方依据：[Docker Compose 部署](https://langfuse.com/self-hosting/deployment/docker-compose)、[上游 Compose](https://github.com/langfuse/langfuse/blob/main/docker-compose.yml)、[OTEL 接入与认证](https://langfuse.com/integrations/native/opentelemetry)。本配置采用 Langfuse v4 的 OTLP HTTP 接口，无需新增 Langfuse SDK。

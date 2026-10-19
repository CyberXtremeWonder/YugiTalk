# YugiTalk 项目说明书

状态：阶段 0–5 已由用户验收通过；阶段 6 生产完善开发中，生产上线尚未完成。

## 文档入口

功能目录、代码入口与中文说明检查见 [模块目录说明](./MODULE_GUIDE.md)。统一启动入口仍为 `backend/start.py`；RAG 与本地嵌入服务源码统一在 `backend/app/rag/`。

[需求](../AGENT_REQUIREMENTS.md)、[README](../README.md)、[架构](./ARCHITECTURE.md)、[API](./API.md)、[测试计划](./TEST_PLAN.md)、[UI 规范](./UI_DESIGN.md)、[阶段 0](./stages/00-design-and-probes.md)、[阶段 1](./stages/01-scaffold.md)。

项目数据位置与迁移验收见 [项目数据目录](./PROJECT_DATA.md)。对话改进总结、提示词与面试讲解见 [Vibe Coding 复盘](./VIBE_CODING_REVIEW.md)。

## 启动与环境

可直接照做的 Windows 启动命令见 [开发端启动说明](./DEVELOPMENT.md)，包含 Docker 8080、本地热更新 5173、账号初始化、Worker/Scheduler、停止服务与排障。

开发环境为 React/Vite、FastAPI、Linux Celery、PostgreSQL + Milvus、Redis、MinIO。首次运行先执行 `scripts/init-project-data.ps1` 初始化持久目录，再在根目录执行 `docker compose up --build -d --wait`，Docker 页面 8080、API 宿主端口 18000；本机开发页面 5173、API 8000，安装及热更新见 README。

后端根路径开发环境跳转 API 文档，生产跳转存活接口。工作台界面请访问本机 Vite 的 5173 或 Docker 前端的 8080；只启动后端不会启动前端。Docker API 宿主端口由 `API_BIND_PORT` 配置，默认 18000，与本机 8000 分开以避免冲突。

`backend/start.py` 支持 `--mode api|worker|scheduler`、`--env development|test|production`、`--host`、`--port`、`--reload`、`--check`。默认 API；预检成功退出 0，配置失败退出 2，只打印脱敏字段说明。Windows Worker 使用 Linux 容器。启动不会安装依赖、迁移或创建管理员。

优先级：CLI 允许的启动参数 > 环境变量 > 根目录 `.env` > 开发默认值。统一配置在 `backend/app/core/config.py`。Compose 的服务内地址由 Compose 配置；`.env.example` 中的 localhost 地址用于本机 API 连接开发容器。

没有 `.env` 时，本机默认端口也是数据库 25432、Redis 26379、MinIO 29000，与开发 Compose 一致。Windows 启动入口显式使用 Selector 事件循环以兼容 Psycopg；内部 MinIO 健康检查直连服务，不使用系统代理。

| 配置组 | 字段 | 当前作用 |
| --- | --- | --- |
| 应用 | APP_ENV、APP_HOST、APP_PORT、APP_RELOAD、LOG_LEVEL | 环境、监听与日志 |
| 依赖 | DATABASE_URL、REDIS_URL、STORAGE_ENDPOINT | 真实依赖连接 |
| 向量存储 | MILVUS_URI、MILVUS_TOKEN、MILVUS_DATABASE | Milvus REST v2 连接与凭证；本机默认 29530 |
| 存储 | STORAGE_ACCESS_KEY、STORAGE_SECRET_KEY、STORAGE_BUCKET | 配置校验；文件业务后续接入 |
| 模型 | MODEL_PROVIDER、MODEL_ID、MODEL_API_KEY、MODEL_BASE_URL | Mock／OpenAI 兼容／Anthropic 适配器已实现；真实联调待配置 |
| 会话与预算 | SESSION_SECRET、CORS_ORIGINS、MAX_MODEL_STEPS、MAX_TOOL_CALLS、WORKSPACE_TOKEN_BUDGET、USER_TOKEN_BUDGET | 会话、授权和用量统计已执行；默认关闭 Token 额度拦截，生产运维继续分阶段交付 |
| 超时 | DEPENDENCY_TIMEOUT_SECONDS、TASK_TIMEOUT_SECONDS | 探测与队列超时 |

`CORS_ORIGINS` 为 JSON 数组，同源代理用空数组；开发密码只供开发。生产拒绝 Mock、空模型密钥、默认模型名、短会话密钥、开发存储凭证、无密码数据库／Redis、DEBUG／热重载及非 HTTPS 跨域来源，并关闭文档接口。配置校验不等于生产部署已完成。

## 迁移与持久化

`python -m alembic upgrade head` 执行迁移，当前版本 `0007_reliability`。Compose 中 migrate 等待 PostgreSQL 健康，API／Worker 同时等待迁移成功与 Milvus 就绪。重复执行不重复创建版本；后续业务表添加新迁移，不改写已有版本。

PostgreSQL、Redis AOF、MinIO、etcd 和 Milvus 分别绑定项目 `data/postgres`、`data/redis`、`data/minio`、`data/etcd`、`data/milvus`。当前已将旧卷数据复制到项目 E 盘目录并验证；BGE-M3 缓存按用户要求保持原位置。`docker compose down` 保留目录；旧卷暂留用于迁移回退。目录初始化、检查和回退见 [项目数据目录](./PROJECT_DATA.md)。正式备份恢复与带业务数据的发布回滚演练在阶段 6 实现；勿对开发库执行 downgrade 删除身份数据。

## 健康、日志与排障

| 现象 | 处理 |
| --- | --- |
| 页面打不开 | `docker compose ps`，检查 8080 端口和 frontend 日志 |
| ready 返回 503 | 查看 checks 中失败依赖及对应容器日志；修复后重新访问 |
| postgres 为 false | 检查连接与 `docker compose logs migrate`，确认迁移成功 |
| Worker 无响应 | 检查 Worker 健康、Redis 和 Worker 日志 |
| 本机 API 连接失败 | 确认 `.env` 的 25432／26379／29000 地址；服务内地址不能直接用于本机 |
| 端口占用 | 停止同项目本机进程或调整映射，不停止其他项目服务 |
| 预检失败 | 按字段说明修复，不把真实连接串或密钥复制到报告 |

`/health/live` 只证明进程存活；`/health/ready` 实际检查依赖，返回 200 或 503，不调用付费模型。MinIO 探测只证明服务就绪，bucket 与文件权限随知识库接入验证。Worker 使用指定节点 Celery ping。

API 日志为 JSON，记录时间、级别、请求 ID、状态和耗时，避免提示词、查询参数、连接串及原始异常。响应有 `X-Request-ID`，错误体携带同一 ID。

`python scripts/check-stage1-infra.py --fault` 会短暂停止并恢复开发 Redis／API，演练时避免同时演示。启动等待允许代理短暂返回 502，最终必须恢复 ready=200。

向量数据库已切换 Milvus，关系数据保留在 PostgreSQL。Milvus 元数据在 etcd，向量数据在 MinIO 的独立 bucket；开发端口 29530／29091。API 就绪包括 postgres、redis、storage、milvus 四项。故障演练现在也会停止并恢复 Milvus。

旧迁移 `0001_vector` 为兼容全新普通 PostgreSQL 加入扩展可用性判断，已有迁移不会重跑；`0002_milvus` 移除旧扩展。普通 PostgreSQL 16 使用原数据卷。迁移版本回退不代表能把 Milvus 数据恢复到旧向量存储；生产回滚必须在阶段 6 定义并演练。

## 用户操作与后续补充

当前可初始化账号、登录／退出、切换空间、管理成员、创建助手版本、进行 Mock 工具对话、停止生成和管理本人历史。真实模型联调待配置；知识与记忆阶段 4，执行恢复阶段 5，工单与写操作审批待全部阶段完成后拓展。正式部署、HTTPS、备份恢复、回滚和安全扫描阶段 6；真实评测、面试演示及发布说明阶段 7。

## 阶段 2 账号与权限

在根目录运行 `docker compose exec api python -m app.identity.bootstrap --email you@example.com --name "你的名字" --platform-admin`。终端交互输入密码，不限制长度，但初始化账号时密码不能为空；不会显示输入，也不把密码写入命令参数。普通用户省略 `--platform-admin`；每个新用户有自己的初始空间。禁止服务启动时自动创建管理员，没有共享默认密码；重复初始化返回非零并保留已有账号。

本机运行方式：进入 backend 后执行 `.venv/Scripts/python.exe -m app.identity.bootstrap --email you@example.com --name "你的名字" --platform-admin`，使用当前本机配置连接数据库。Compose 与本机 API 应使用相同 `SESSION_SECRET` 才能共享同一数据库的会话。

登录成功后 Cookie 为 HttpOnly、SameSite=Lax；开发 HTTP 不设置 Secure，生产 HTTPS 必须设置 Secure。会话默认 8 小时，可用 `SESSION_TTL_SECONDS` 调整（60–604800 秒）；轮换 `SESSION_SECRET` 会使旧会话与 CSRF Token 失效。`AUTH_TRUSTED_ORIGINS` 是浏览器实际 origin 的 JSON 数组，写请求必须同时匹配来源和 `X-CSRF-Token`；不要加路径、通配符或容器内部 URL。

空间 owner／admin 可以管理普通成员，admin 不能管理 owner 或授予 owner。最后一个 owner 不能降级或移除；并发修改通过 PostgreSQL 行锁保护。普通成员仅能查看自己加入的空间。平台管理员可以查询审计接口，仍不能越过空间成员关系。

登录使用真实 Redis 原子计数：15 分钟内每账号最多 10 次、每连接来源最多 30 次，包含成功尝试；超限 429（Retry-After=900），Redis 不可达返回 503。当前开发 Nginx 后连接来源为代理地址，来源限额会共享；生产可信代理与真实客户端 IP 策略在阶段 6 完成，不能直接信任客户端自报转发头。

`./scripts/test-stage2.ps1 -IncludeContainers -IncludeUI` 在开发依赖上创建随机独立测试数据库和临时应用容器，端口 18080，结束清理。测试密码随机生成，不写入环境文件或正式文档。浏览器失败轨迹在 `.cache`，用于调试，勿发布可能包含测试凭证的轨迹。项目 CI 也运行该隔离测试。

## 阶段 3：助手和会话操作

使用已有账号登录工作台。工作空间 owner／admin 在 Agent 页创建助手或发布新版本，配置中文提示词、只读工具、预算、模型步数、工具次数、输出上限和任务超时。普通成员可以使用已发布助手。

在 Agent 页选中助手后可点击“删除助手”并确认；助手会从目录移除，不能再发布版本或创建新任务。历史会话、版本和任务保留，已有任务可继续执行。部署此功能前须执行 `0018_agent_deletion` 迁移。

模型协议流默认最大 4 MiB（`MODEL_MAX_STREAM_BYTES=4194304`），不与对话 Token 额度混算；单帧、可见正文及工具参数分别保留 64 KiB 大小保护。流式中断排查与本轮验证见 [响应流修复说明](./stages/06-stream-size-fix.md)。

回到“新建对话”，选择助手并发送任务。当前默认 Mock：回复、文本统计和报告排版用于离线演示，不代表真实模型输出。界面显示工具状态、Markdown／代码块、最终状态和估算用量，未知 Token 不显示为零。Enter 发送，Shift + Enter 换行。开发与生产默认关闭单次及累计 Token 额度拦截，仍保留用量统计、并发、输出上限、步骤和超时边界；配置见 [部署说明](./DEPLOYMENT.md)。

左侧历史打开本人会话；“管理对话”可重命名或删除，执行中不能删除。任务记录只显示本人运行。展开详情显示当前会话的真实状态、模型和用量。停止生成显式取消后续调用；关闭页面或断网只断开事件读取，任务继续执行，回到历史可继续查看。

完整自动验收：`pwsh -NoProfile -File scripts/test-stage3.ps1 -IncludeContainers -IncludeUI`。该命令复用开发基础服务，但使用随机独立数据库、测试账号、独立 Worker 队列，测试结束自动清理临时资源。

真实供应商配置、验收步骤与剩余限制见 [阶段 3 报告](./stages/03-agent.md)。密钥只在根目录 `.env` 填写，配置后重建 API／Worker 并发布新助手版本；没有真实联调时不确认阶段整体通过。

## 阶段 4：知识与记忆

知识库页支持创建、上传、索引状态、重建、失败重试、删除和检索预览。对话输入框选择知识库后，正文隐藏 `[S2]` 等索引，回答下方展开“检索来源”并点击文件名核对原文，内部证据身份和权限校验保留。文档删除立即停止召回，旧来源随后不可查看。支持 TXT、Markdown、文本 PDF；扫描 PDF 需要后续 OCR。

设置页“个人记忆”仅保存用户主动确认的偏好，可修改、删除或关闭。关闭后不读取、不写入、不注入。历史摘要为有界抽取式摘要，不额外调用模型。

默认嵌入使用本地 Hugging Face 缓存 BGE-M3；服务器模型可配置兼容接口。聊天模型的 DeepSeek 配置独立。完整配置、测试和人工验收见 [阶段 4 报告](./stages/04-knowledge.md)。

每阶段下载缓存清理是收尾必做项。安装前执行 `. ./scripts/set-project-cache.ps1`；C 盘本阶段新增 uv 包缓存通过 `scripts/clean-stage-cache.ps1` 定向清理，未指定包名时从项目锁文件读取依赖包名。释放空间记录在阶段 artifacts，不清理模型或已安装依赖。

## 阶段 5 只读工具与后台恢复

Agent 页授权文本统计、报告排版并发布新版本。新建对话选择该版本，让模型调用只读工具，公开流程展示工具与恢复状态。任务记录页保留历史任务和确认恢复入口。

创建工单、配套审批卡及工单列表已移除，待项目全部完成后再拓展；历史版本与数据保留，旧配置会过滤下线工具。

运行恢复由独立 Scheduler 补投排队状态、回收失效租约。可恢复的模型错误在任务记录页显示确认恢复入口，提示中断模型调用可能再次计费；服务端仍核对预算、并发及次数上限。没有外部模型回复时，不会把 Mock 当作真实供应商验证。

启动恢复调度器：`backend/.venv/Scripts/python.exe backend/start.py --mode scheduler`；Docker Compose 默认已经包含 Scheduler。配置、自动故障注入和人工验收见 [阶段 5 报告](./stages/05-reliability.md)，排障见 [RUNBOOK](./RUNBOOK.md)，面试展示见 [DEMO](./DEMO.md)。

上传资料后，每个文件显示导入步骤和两条分块进度：Embedding 已完成数/总数、Milvus 已写入数/总数。按真实批次更新，通常每 2 秒刷新。出错步骤标红并显示安全原因；点击“重试”开启新版本索引，成功批次数不会被当作完整成功。

## 文件夹与导入布局

左侧“上传文件夹”递归读取目录中的 TXT、Markdown 和 PDF，按文件逐个上传，每文件最多 20 MB，一批最多 200 个支持文件；不支持格式跳过。批量统计显示成功、失败与跳过数量；失败项目显示本地相对路径和原因。索引依旧由服务器异步完成。

左侧显示各文件竖向流程，右侧显示知识库文件及检索。成功后流程自动清空，文件保留；失败步骤可定位，右侧可重试。删除成功后文件即从页面隐藏，后台继续清理数据，失败任务可在左侧重试清理。手机自动上下排列。

## 用户自定义 Skill

左侧 Skill 页面支持创建、版本发布、启停和删除，技能在工作空间内共享。回到对话页，在输入框底部选择技能后发送；只使用 Agent 和 Skill 共同授权的工具。编辑不改变已有任务，停用或删除会阻止后续执行与恢复。操作示例、接口、数据位置及隔离验收步骤见 [自定义 Skill](./CUSTOM_SKILLS.md)。

## 阶段6生产完善（部署状态）

Prompt 注入防护默认生效，无额外模型、下载或环境开关。技能、摘要与记忆不会获得系统权限；对话图片屏蔽、链接仅显示文字，受控来源卡片继续可用。代码说明与验收见 [防护讲解](./PROMPT_INJECTION_DEFENSE.md)。

生产配置、HTTPS、监控、项目内冷备恢复与镜像发布工具已实现；整体阶段未通过，安全与完整评测门槛仍需处理。当前实现、验证与剩余项见 [阶段6报告](./stages/06-production.md)，准确部署命令、预算边界与数据操作见 [生产部署说明](./DEPLOYMENT.md)。正式服务器与真实模型质量不能用隔离协议替身代替。

正式对象存储选择现有服务器/云端S3，接入字段和检查命令见 [S3说明](./S3_SETUP.md)。当前缺少服务信息，仅完成本机协议替身验证；远端对象恢复未实现时，本机备份和依赖它的发布脚本会在停机前拒绝操作。不要把这个明确的未完成门槛当成Docker故障或绕过它。

## Tool 管理

登录后左侧 Tool 显示默认五项工具（含 Tavily 网页搜索）。网页搜索需配置 `TAVILY_API_KEY` 和 `api.tavily.com` 白名单。天气路况待配置时先按 [Tool 管理说明](./TOOL_MANAGEMENT.md) 配置服务端 Key 和白名单，再发布 Agent/Skill 授权。用户可提交个人工具审核，平台管理员批准后仍绑定提交者；其他用户不能查看或调用。普通用户不能直接创建或发布工具绕过审核。本人可启停或删除个人工具，参数校验不访问外部服务。邮箱验证码、开发模式与管理员面板见 [身份与审核说明](./REGISTRATION_AND_TOOL_REVIEW.md)。

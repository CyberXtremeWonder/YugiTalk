# 单机生产部署与数据操作

阶段 6 已实现部署、HTTPS、监控、冷备恢复与镜像发布工具；当前仍有安全与完整评测门槛未通过，不能直接作为正式上线批准。状态以 [阶段报告](./stages/06-production.md) 为准。

## 环境与目录

需要 Docker Linux 容器、Compose ≥ 2.24.4、Python 3.12 和 Node ≥ 22.12（源码构建时）。Windows 使用 Docker Desktop；服务器使用 Linux Docker Engine。业务数据使用项目相对目录 `data/production/`；开发数据仍在 `data/{postgres,redis,minio,etcd,milvus}`，不能混用。缓存放 `.cache/`。BGE-M3 缓存继续使用已有外部目录，在服务器填写服务器自身的绝对路径，不复制 Windows 路径。

生产最终配置只开放前端 HTTP/HTTPS 入口。关系库、队列、对象存储、向量库、嵌入服务与监控服务没有宿主端口，Milvus 启用认证。首版是单机部署，需要停机冷备，不提供多机高可用承诺。

2026-10-11起，API限额明确设为2个CPU，Worker默认配置仍为2个进程。20并发稳定性验收另用8进程Worker/2 CPU测试覆盖；报告记录实际镜像、CPU和内存限额，不能将测试容量表述为默认正式容量。Worker启动时加载执行图依赖，各子进程延迟创建独占连接池并在退出时释放，任务会话仍逐次关闭；配置更新通过重建服务生效。

## 首次部署

2026-10-12 用户决定暂不执行恢复演练，先准备首次部署。统一入口与从空环境开始的操作见 [首次部署步骤](./FIRST_DEPLOYMENT.md)，支持只读 `--check`。它不调用备份或恢复流程，仍核对真实S3、HTTPS与服务健康；这一范围调整不代表恢复能力、安全或真实模型评测已通过。现有升级发布工具的备份边界保持原样。

1. 安装锁定依赖：Windows 先执行 `. ./scripts/set-project-cache.ps1`，再在 backend 运行 `uv sync --frozen`。Linux 在项目根执行 `UV_CACHE_DIR="$PWD/.cache/uv" uv sync --project backend --frozen`。
2. 复制 `.env.production.example` 为 `.env.production`，分别生成至少32位随机内部密码和令牌；不要复用开发凭证。填写真实模型 HTTPS 地址、模型名称、密钥、独立嵌入认证、HTTPS 域名和 BGE 缓存路径。不能使用测试替身或 Mock。
   正式对象存储使用现有 S3：填写端点、Region、两个预置专用 Bucket、服务方凭证与可选私有 CA，操作见 [S3接入说明](./S3_SETUP.md)。没有服务信息时不要使用测试存储配置发布。
3. 构建发布镜像。安全更新会访问发行版仓库；最终镜像 ID、软件包清单和漏洞报告应一起保存，不能仅根据可变标签判断版本。

```powershell
backend/.venv/Scripts/python.exe scripts/build-production-images.py
backend/.venv/Scripts/python.exe scripts/check-production-s3.py --env-file .env.production --write-probe
backend/.venv/Scripts/python.exe scripts/init-production.py --env-file .env.production
```

构建入口在重建标签前先把实际旧ID保留为每组件一个 `build-previous` 标签；不要并行重建同一标签。正式发布仍应使用独立版本标签/固定ID，压测与回滚的专用保留标签不轮换。

Linux 把 Python 路径替换为 `backend/.venv/bin/python`，其余 Docker/Python 参数相同。初始化脚本拒绝开发数据目录、路径逃逸、重解析路径、弱凭证以及覆盖已有私有配置。

4. 把对应域名的受信任证书链与私钥放入 `data/production/tls/fullchain.pem` 和 `privkey.pem`。私钥不得提交或放入镜像。设置非 root 服务目录权限，再核对两份文件合并后的配置并启动：

```powershell
backend/.venv/Scripts/python.exe scripts/init-production.py --env-file .env.production --permissions
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml config --quiet
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml up -d --wait --wait-timeout 300
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml exec api python -m app.identity.bootstrap --email you@example.com --name "管理员"
```

最后一条交互输入管理员密码，不把密码写在命令行。生产没有默认管理员、公开注册或 API 调试文档。迁移服务成功后才启动应用；首次 PostgreSQL 健康检查使用 TCP，避免初始化临时实例过早触发迁移。

正式域名证书、DNS、服务器权限和服务器故障测试尚未在本项目环境验证。隔离验收使用短期自签证书；该证书不能用于正式域名。对企业自签模型服务可设置 `MODEL_CA_FILE`，并用独立 Compose 覆盖文件只读挂载 CA；模型连接始终验证证书，生产地址必须为 HTTPS。

## 预算与监控

开发与生产默认关闭对话 Token 额度拦截：`TASK_BUDGET_ENABLED=false`、`CUMULATIVE_BUDGET_ENABLED=false`。不因单次预留或空间、用户累计用量耗尽拒绝任务，仍记录保守用量和供应商返回的 Token。空间并发4、用户并发2，模型输出上限、上下文容量、任务超时、步骤和工具次数继续生效。以后需要预算保护时可显式开启两个开关；仅开启时才使用空间、用户累计默认100000及助手的单次预算配置，累计额度不自动按日重置。

Prometheus 每15秒抓取 API 内部指标，以独立 `METRICS_TOKEN` 认证。代理禁止访问 `/internal/`。指标只记录模板路由、状态、响应头耗时和任务聚合；任务 Token 汇总不重复计算空间与用户两份预算。

Jaeger 接收 API → 队列 → Worker → 模型/工具步骤的 OTLP 追踪。默认采样10%，批次队列2048、导出超时2秒；不采集提示词、模型正文、工具参数或原始异常消息。SDK 线程及上下文增加运行成本。Prometheus 与 Jaeger 业务文件也位于项目目录，保留14天；Docker JSON 日志每服务10MB×3轮转。告警规则覆盖 API 抓取失败和 HTTP 错误比例，目前没有配置外部通知渠道，也没有向他人发送消息。

## 冷备和恢复

以下本机冷备恢复已经在隔离环境验证。切换现有外部S3后，脚本会在停止服务前拒绝操作；对象版本/快照与数据库恢复点、远端恢复工具及真实演练尚待服务信息明确后实现。以下正式发布命令也因此尚不可用于外部S3，不能绕过备份门槛。[详细范围](./S3_SETUP.md)

冷备会停止前端、API、Scheduler、Worker，再停止跨存储写入服务，逐文件复制并核对 SHA256；无论成功失败都启动停机前实际镜像 ID。不能在另一进程同时发布、备份或执行数据操作。选择维护窗口；建议至少每日执行一次完成的冷备，并复制到受控的异机加密存储，才能覆盖主机丢失风险。当前脚本没有自动异机复制和每日调度，RPO≤24小时取决于该运维安排。

```powershell
backend/.venv/Scripts/python.exe scripts/production-data.py backup --env-file .env.production --project yugitalk-prod
backend/.venv/Scripts/python.exe scripts/production-data.py restore --snapshot data/production/backups/时间戳 --target data/production-restored
```

第二条只能向全新、未被任何容器挂载的项目子目录恢复，不覆盖运行数据。备份含 PostgreSQL、Redis、MinIO、etcd、Milvus、私有配置、TLS、Prometheus、Jaeger和私有S3 CA共十类目录；因此也包含密码和私钥，必须控制目录访问与外部备份权限。BGE 权重不在备份中。快照清单冻结实际后端、前端镜像ID，复制和恢复工具也使用冻结版本；不得在清理镜像时删除仍需恢复的版本。

恢复后创建受控生产环境文件，把 `PROD_DATA_ROOT` 改为新目录；先停止旧环境，再按首次部署命令启用新目录。核对迁移版本、账户、任务、原文散列、真实向量召回和知识库问答。仅有文件散列一致不足以证明业务恢复成功。没有自动删除历史备份的行为，保留期限由运维确定，磁盘空间应定期检查。

## 镜像升级和回滚

准备已扫描的镜像，明确新旧版本同时兼容的数据库迁移版本。发布脚本先验证当前镜像仍能重新创建、添加本地保留标签，再冷备并保存实际镜像 ID，最后部署和等待健康；失败自动恢复原镜像。此工具只适用于兼容当前 Schema 的版本，不能自动撤销迁移或把备份覆盖回运行库。

```powershell
backend/.venv/Scripts/python.exe scripts/deploy-production.py --env-file .env.production --backend-image yugitalk-backend:0.2.0 --frontend-image yugitalk-frontend:0.2.0 --compatible-schema 0007_reliability
backend/.venv/Scripts/python.exe scripts/deploy-production.py --env-file .env.production --compatible-schema 0007_reliability --rollback
```

操作记录位于 `data/production/backups/last-release.json`；只能回退这一记录中的上一组镜像，旧记录需另行归档。数据库版本不匹配时拒绝操作；破坏性变更采用先兼容再清理方案，单独演练。项目镜像清理脚本保护全部容器引用、现用发行标签、发布记录、构建上一版和快照清单中的固定镜像；它只定向清理项目构建残留，不使用全局 prune。带版本标签的多余镜像需先核对恢复用途，再按确切标签删除；未采用的第三方候选另核对其没有容器引用，不删除整个公共仓库。

## 隔离验收和停止服务

```powershell
pwsh -NoProfile -File scripts/test-stage6.ps1 -IncludeProduction -IncludeRestore -IncludeUI
```

该命令生成随机私有测试文件 `.cache/stage6-production.env`、独立数据和 `yugitalk-stage6` 项目。模型协议替身用于 HTTPS、SSE 和部署验证；嵌入与向量库使用真实 BGE-M3/Milvus。测试保留 `https://localhost:18443` 供查看，浏览器需要信任短期测试证书。浏览器登录基线账号为 `stage6@example.test`；每轮功能验收另用独立账号，邮件地址保存在 `docs/stages/artifacts/stage6-production.json` 的 `functional.email`，避免历史额度影响重复验收。密码是私有环境文件里的 `E2E_PASSWORD`，不能复用于开发或生产。

停止测试环境时使用三个文件：

```powershell
docker compose --env-file .cache/stage6-production.env -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.stage6-test.yml -p yugitalk-stage6 down
```

正式环境停止使用首次部署的两份文件和 `.env.production` 执行 `down`。绑定目录不会随 down 删除；不提供自动删除业务目录的卸载命令。

## 实现参考

- [Compose 合并和清空规则](https://docs.docker.com/reference/compose-file/merge/)：本项目显式清空开发构建和端口。
- [Milvus 2.5 认证](https://milvus.io/docs/v2.5.x/authenticate.md)：生产启用认证与独立 root 密码。
- [OpenTelemetry Python 导出](https://opentelemetry.io/docs/languages/python/exporters/)：有界批次导出。
- [Jaeger 2.22 Badger 配置](https://github.com/jaegertracing/jaeger/blob/v2.22.0/cmd/jaeger/config-badger.yaml)：单机持久追踪，验收查询使用 v3 API。

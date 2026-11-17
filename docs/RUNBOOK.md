# 运行监控与故障排查

更新日期：2026 年 11 月 13 日。安装和热更新步骤统一见 [DEVELOPMENT](DEVELOPMENT.md)，首次生产与升级分别见 [FIRST_DEPLOYMENT](FIRST_DEPLOYMENT.md) 和 [DEPLOYMENT](DEPLOYMENT.md)。本文合并原启动监控和演示检查说明。

## 先确认操作环境

| 环境 | 项目与配置 | 入口与注意事项 |
| --- | --- | --- |
| 本机开发 | yugitalk-dev，`.env` 和基础 Compose；可叠加 GPU | 默认 8080，API 18000 |
| 公网网关演示 | 同一个 yugitalk-dev 加 gateway 覆盖和 SSH 隧道 | 与本机共享数据；不是独立生产 |
| 正式生产 | yugitalk-prod，`.env.production` 加生产覆盖 | 使用实际部署域名与独立数据 |
| 阶段六验收 | yugitalk-stage6，私有测试环境与测试覆盖 | 默认 18443；协议替身和测试账号不能用于生产 |

操作前用 `docker inspect <容器名>` 的 Compose 标签核对原配置文件。GPU 或网关环境后续维护要继续使用相同覆盖文件；不要用普通启动命令无意替换 GPU 镜像或公网代理。

## 日常监控

以下是基础开发环境命令，GPU 环境在 compose 后添加 `-f docker-compose.yml -f docker-compose.gpu.yml`；网关配置按 [TENCENT_GATEWAY](TENCENT_GATEWAY.md) 保留。

```powershell
docker compose ps -a
docker compose logs -f --tail 100 api worker scheduler
docker compose logs --tail 100 migrate frontend embedding docling
curl.exe -f http://127.0.0.1:8080/health/ready
$taskContainers = @(docker ps -q --filter label=com.docker.compose.project=yugitalk-dev)
if ($taskContainers.Count -gt 0) { docker stats $taskContainers }
```

日志与资源监控使用 Ctrl+C 退出，不停止服务。`live` 只证明进程存活，`ready` 检查依赖，均不证明模型问答质量。任务用 `run_id`、请求用 `request_id` 和 `trace_id` 定位，不公开密钥、Cookie、用户原文或连接串。

正式生产使用原来的环境文件、项目名和全部覆盖文件。例如：

```bash
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml -p yugitalk-prod ps -a
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml -p yugitalk-prod logs --tail 100 migrate api worker scheduler
```

Prometheus、Trace、Langfuse 和可选邮件告警分别见 [OBSERVABILITY](OBSERVABILITY.md)、[LANGFUSE](LANGFUSE.md)、[ALERTING](ALERTING.md)。存在配置工具不代表当前邮箱告警已经启用。

## 配置与代码更新

- 启动已经停止的旧容器可使用原 Compose 参数执行 `start`；这不会创建缺失服务或更新镜像。
- 仅修改环境变量时重新创建 `api worker scheduler`；`restart` 不更新容器环境，本机 API 也需要重启。
- 修改前后端代码时先构建，再更新镜像；不要只重启旧容器。
- 更新前保留旧镜像和必要的数据备份，检查新迁移与回退兼容性。生产升级不能绕过现有外部 S3 备份限制。

开发 GPU 环境示例：

```powershell
$composeArgs = @('compose', '-f', 'docker-compose.yml', '-f', 'docker-compose.gpu.yml')
& docker @composeArgs config --quiet
& docker @composeArgs build api frontend
& docker @composeArgs up -d --no-build --wait --wait-timeout 600
```

不要在没有 GPU 配置的主机照抄 GPU 覆盖；普通开发使用基础文件。首次生产脚本只允许空目标，不用于日常升级。

## 常见故障

| 现象 | 检查与处理 |
| --- | --- |
| 页面打不开 | 核对端口、frontend 和 api 状态；后端根路径不是聊天工作台 |
| ready 返回 503 | 查看失败依赖，再定位 PostgreSQL、Redis、对象存储或 Milvus |
| migrate 失败 | 查看迁移日志并查询 alembic_version，不先跳过迁移或删除数据库 |
| 任务一直 queued | 检查 Redis、Worker 队列与 Scheduler，数据库保存的 queued 意图可以补投 |
| 任务一直 running | 检查 Worker 租约和日志；调度器在租约到期后从检查点恢复 |
| 任务 paused | 用户主动暂停，只有继续操作才恢复；失联也保留暂停状态 |
| 任务 waiting_input | 回答澄清或取消；超期不能恢复，不应使用 steer 代替 clarify |
| 插话已收到但未完成 | 已持久化不代表执行成功；检查后续模型步骤、预算、错误和状态 |
| 工具显示配置不足 | 核对后台密钥和白名单，并让 API 与 Worker 同时重新加载配置 |
| 文档失败 | 区分解析、NUL、分块入库、嵌入、索引和时限，见 DOCUMENT_CHUNKING |
| SSE 断开 | 从原游标重连，关闭页面不取消任务；准入过载和权限撤销也可能关闭流 |

模型中断后可能重新生成并计费；MCP 或外部操作结果不确定时先核对远端，不能盲目重放。可恢复失败使用任务记录的明确恢复入口，权限撤销和下线工具不能直接绕过。

## 网关链路排查

本机前端就绪后，再检查云端 Caddy、隧道监听、域名和证书。腾讯云入口不承载整套模型与数据库，配置见 [网关指南](TENCENT_GATEWAY.md)。

```bash
sudo systemctl status caddy --no-pager
sudo journalctl -u caddy --since "5 minutes ago" --no-pager
ss -lnt | grep 18080
curl -f http://127.0.0.1:18080/health/ready
```

无监听先检查本机 SSH 窗口；隧道健康但公网失败再查 Caddy、DNS 和证书。不要关闭承担隧道的窗口，不在日志中贴私钥或保护口令。公网是否可用需要当场验证，不能根据旧报告推断。

## 停止与数据保护

```powershell
docker compose stop frontend api worker scheduler
# 或停止整个开发项目，保留数据。
docker compose stop
```

生产使用原参数执行 stop；关闭网关时先断开隧道，必要时再停止云端入口。不要用 `down -v` 或递归删除 data 目录排障。文档索引宕机后会增加版本并从原文重建，重试不是逐块断点续算。

备份、恢复、发布串行执行。外部 S3 的对象恢复点与关系库备份需要配套验证，不能以本机 MinIO 目录替代云端备份。阶段六未完成项仍以对应报告为准。

## 隔离演练与演示检查

```powershell
pwsh -NoProfile -File scripts/test-stage5.ps1 -IncludeContainers -IncludeUI
```

隔离故障脚本针对专用数据库、队列和测试 Worker，不能手动改成真实业务目标。缓存清理不包含业务数据和模型权重。

演示时依次展示用户隔离、助手版本、文档导入与来源、授权工具、暂停继续和插话、任务记录与恢复。使用独立测试资料；停止后确认状态与已公开正文，工具恢复核对账本结果。模型再次调用可能计费，不能把 Mock 演示、检索命中或受控 503 拒绝写成真实答案质量或高并发成功。

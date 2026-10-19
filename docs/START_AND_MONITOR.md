# 开发端、公网演示端与生产端：启动和后端监控

更新日期：2026-10-12。命令按执行位置分开，避免将本机 PowerShell 命令输入腾讯云 Linux 终端。

## 环境区别

| 环境 | 容器前缀 | 配置 | 入口 |
| --- | --- | --- | --- |
| 本机开发 | yugitalk-dev | .env + docker-compose.yml | http://127.0.0.1:8080 |
| 当前公网演示 | yugitalk-dev（与本机开发共用服务和数据） | 开发配置 + docker-compose.gateway.yml | https://yugiagent.fun |
| 正式生产 | yugitalk-prod | .env.production + 开发基础及生产覆盖文件 | 生产配置中的实际 HTTPS 域名 |
| 阶段6验收 | yugitalk-stage6 | 独立测试配置 | https://localhost:18443 |

目前公网域名通过腾讯云 Caddy、SSH 隧道访问本机 Docker。它不等于已部署独立生产环境。开发与公网演示是同一组容器的两种入口配置，不要同时当作两套环境启动。正式生产启动流程不适合当前2GB网关服务器。

## 一、开发端启动（Windows PowerShell）

先启动 Docker Desktop，再打开新的 PowerShell：

```powershell
cd E:\Code\YugiTalk
. ./scripts/set-project-cache.ps1
./scripts/init-project-data.ps1
docker compose -p yugitalk-dev config --quiet
docker compose -p yugitalk-dev --profile local-embedding up -d --wait --wait-timeout 600
```

前提：已有 `.env`、已有应用镜像和本地 BGE-M3 缓存。首次安装或代码更新需要构建时，将启动命令增加 `--build`；构建会访问依赖和镜像源。使用远端嵌入模型时不启用 `local-embedding`，并检查 `.env` 的 COMPOSE_PROFILES。不要覆盖现有私有配置。BGE-M3 缓存仍使用 `E:/Code/HUGGINGFACE_CACHE/models--BAAI--bge-m3`。

本机有 NVIDIA 独立显卡时，完成基础启动后运行 `./scripts/start-local-embedding.ps1 -LocalOnly` 自动选择 GPU，没有可用 GPU 则使用 CPU。当前公网网关演示使用 `./scripts/start-local-embedding.ps1`，保留网关覆盖配置。以后基础 Compose 启动可能恢复 CPU 镜像，需再次运行此脚本选择显卡。查看实际设备：`Invoke-RestMethod http://127.0.0.1:18081/health`。详见 [本机 GPU 嵌入说明](./LOCAL_GPU_EMBEDDING.md)。

从公网演示切回本机开发时，先退出 SSH 隧道，再恢复原始配置：

```powershell
docker compose -p yugitalk-dev up -d --force-recreate api worker scheduler frontend
```

打开 http://127.0.0.1:8080。后端文档为 http://127.0.0.1:18000/docs。本地代码热更新方式见 [开发说明](./DEVELOPMENT.md)。

### 开发端后端监控

```powershell
cd E:\Code\YugiTalk
# 查看全部容器，包括失败后退出的迁移容器。
docker compose -p yugitalk-dev ps -a
# 实时查看请求处理、后台任务和调度日志。
docker compose -p yugitalk-dev logs -f --tail 100 api worker scheduler
# 查看数据库迁移失败原因。
docker compose -p yugitalk-dev logs --tail 100 migrate
# 检查依赖就绪。
curl.exe -f http://127.0.0.1:18000/health/ready
# 只监控当前开发项目的资源占用。
$devContainers = @(docker ps -q --filter label=com.docker.compose.project=yugitalk-dev)
if ($devContainers.Count -gt 0) { docker stats $devContainers }
```

健康结果应为 `ready`，各依赖检查为 `true`。日志和资源监控用 Ctrl+C 退出，不会停止服务。

### 开发端停止

```powershell
# 只停止业务与后台任务，保留依赖运行。
docker compose -p yugitalk-dev stop frontend api worker scheduler
# 停止整个开发项目，容器和持久数据保留。
docker compose -p yugitalk-dev stop
```

## 二、当前公网演示端启动

### 1. 本机启动 Docker（Windows PowerShell）

```powershell
cd E:\Code\YugiTalk
docker compose -p yugitalk-dev -f docker-compose.yml -f docker-compose.gateway.yml config --quiet
docker compose -p yugitalk-dev -f docker-compose.yml -f docker-compose.gateway.yml --profile local-embedding up -d --wait --wait-timeout 600
curl.exe -f http://127.0.0.1:8080/health/ready
```

使用已有安全配置、独立会话密钥与已更新的管理员密码。网关覆盖配置同时允许 `https://yugiagent.fun` 和明确列出的本机入口（包括 `http://127.0.0.1:8080`），支持本机开发与公网演示。HTTPS 入口的会话 Cookie 带 Secure 标志，本机 HTTP 入口不添加该标志；来源校验仍然生效。

### 2. 云端启动 HTTPS 入口（腾讯云 Ubuntu 终端）

首次配置参考 [腾讯云网关指南](./TENCENT_GATEWAY.md)，完成后日常启动：

```bash
sudo systemctl start caddy
sudo systemctl status caddy --no-pager
```

### 3. 本机启动加密隧道（另开 Windows PowerShell）

```powershell
cd E:\Code\YugiTalk
ssh -N -T -p 22 -i ./data/gateway/tunnel_ed25519 -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -R 127.0.0.1:18080:127.0.0.1:8080 yugitalk-tunnel@1.117.231.2
```

IP、端口若变更需要替换；首次连接核对服务器指纹。输入密钥保护口令后，无输出且窗口持续运行通常表示隧道工作。保留窗口，电脑不能休眠。域名入口：https://yugiagent.fun。

### 公网演示后端监控

本机 API、Worker 等仍使用上面的开发端监控命令。腾讯云终端额外执行：

```bash
# 查看最近五分钟入口错误。
sudo journalctl -u caddy --since "5 minutes ago" --no-pager
# 实时入口日志。
sudo journalctl -u caddy -f
# 检查隧道监听，只应为127.0.0.1:18080。
ss -lnt | grep 18080
# 分别验证隧道和公网链路。
curl -f http://127.0.0.1:18080/health/ready
curl -f https://yugiagent.fun/health/ready
```

`18080 connection refused` 通常表示隧道断开；先查看 Windows SSH 窗口。监听存在但健康检查失败时检查本机 Docker；隧道健康正常但公网失败时检查 Caddy、DNS、证书和防火墙。

### 公网演示停止

1. 本机 SSH 窗口按 Ctrl+C，断开公网到本机的连接。
2. 本机执行 `docker compose -p yugitalk-dev stop`，停止项目。
3. 如需停止云端入口，腾讯云执行 `sudo systemctl stop caddy`。

## 三、正式生产端启动（整套项目部署主机）

只有准备了能够承载整套项目的主机、独立生产数据、受信任证书、真实 S3 两桶和完整 `.env.production` 时，才执行本节。不要在当前2GB腾讯云网关上启动整套生产栈。完整准备与镜像构建步骤见 [首次生产部署](./FIRST_DEPLOYMENT.md)。

### 首次部署（Linux）

```bash
cd /opt/YugiTalk
# 只读预检。
backend/.venv/bin/python scripts/start-production.py --env-file .env.production --project yugitalk-prod --check
# 预检通过且所有外部资源准备好后，执行首次部署。
backend/.venv/bin/python scripts/start-production.py --env-file .env.production --project yugitalk-prod
```

脚本只用于空数据目录、尚无生产容器的首次部署；会验证真实 S3 与 HTTPS，不执行恢复演练。Windows 运行脚本时将 Python 路径换成 `backend/.venv/Scripts/python.exe`。首次部署生成的镜像固定文件由脚本管理，不要绕过它随意重建。

### 已部署后的日常启动（Linux）

只启动原有停止的容器，保持首次发布固定的镜像和配置：

```bash
cd /opt/YugiTalk
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml -p yugitalk-prod start
```

`start` 不创建缺失容器、不升级镜像、不应用新环境变量；它可能重新启动已经退出的迁移容器，需要检查迁移日志。若配置或代码变化，按正式部署/升级指南处理；不要把 `start-production.py` 当作日常启动命令。

### 正式生产后端监控（Linux）

```bash
cd /opt/YugiTalk
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml -p yugitalk-prod ps -a
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml -p yugitalk-prod logs -f --tail 100 api worker scheduler
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml -p yugitalk-prod logs --tail 100 migrate frontend milvus
# 只监控生产项目，不混入开发或验收容器。
prod_containers=$(docker ps -q --filter label=com.docker.compose.project=yugitalk-prod)
if [ -n "$prod_containers" ]; then docker stats $prod_containers; fi
# 此地址仅适用于生产配置确实使用该域名的情况。
curl -f https://yugiagent.fun/health/ready
```

正式生产若使用其他域名，替换健康检查地址。网页管理员开发面板与任务记录可查看已有运行信息、审计、工具调用和失败原因；健康检查成功不能代替完整功能验收。当前尚未配置自动告警。

### 正式生产停止（Linux）

```bash
cd /opt/YugiTalk
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml -p yugitalk-prod stop
```

## 操作注意

- 启动和监控新开终端，不要关闭承担 SSH 隧道的窗口。
- `stop` 保留容器和数据；不要用删除数据卷或递归删除项目数据的方式排障。
- 不把 `.env`、私钥、SMTP 授权码、Cookie 或用户原文贴进公开日志。
- 这里记录了操作方式；当前真实公网的功能、安全和持续可用性仍需实际验收，正式生产环境未因此被标记为完成。

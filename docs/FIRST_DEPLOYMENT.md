# 首次部署操作步骤

2026-10-12 用户决定暂不执行恢复演练，先准备部署文件。目前没有服务器信息，因此本轮没有连接服务器、购买资源或对外发布。首次部署入口不执行备份或恢复，也不把恢复能力标记为已验证；原有升级和备份工具的边界保持原样。

## 1. 部署位置与准备条件

`scripts/start-production.py` 适用于承载整套项目的主机，不适用于只提供访问入口的 2GB 小服务器。保留本地 BGE-M3 和 Milvus 时，建议先以 4 核、16GB 内存及足够的 SSD 容量规划，再按资料量和实际负载验证；这是规划建议，不是已验证的最低配置。每月30元方案应让现有电脑承担计算，廉价云服务器仅作为访问入口，隧道和云端入口需根据实际机器另行配置。

开始前准备：

- Linux Docker Engine 和 Compose ≥ 2.24.4；安装方式按服务器发行版选择。
- Python 3.12、uv；前端在服务器源码构建时使用 Node ≥ 22.12。
- 项目完整源码及锁文件，项目目录例如 `/opt/YugiTalk`。
- 已配置 DNS 的实际域名与受信任 HTTPS 证书链、私钥。
- 真实 S3 HTTPS 端点、Region、原文和 Milvus 两个预置专用 Bucket、最小权限凭证。
- 模型 API 配置；本机 BGE-M3 缓存路径，或可访问的远端嵌入服务。

不会自动购买服务器、注册域名、申请证书、创建云存储 Bucket 或下载 BGE 模型。已有开发数据库不会自动迁移，首次生产环境是独立的空数据库。

## 2. 安装后端操作依赖

Linux 在项目根目录执行：

```bash
cd /opt/YugiTalk
UV_CACHE_DIR="$PWD/.cache/uv" uv sync --project backend --frozen --python 3.12
```

Windows 仍可用 `backend/.venv/Scripts/python.exe` 执行相同的 Python 脚本；服务器命令中的路径改为自己的项目位置。不要把 Windows 的 E 盘路径复制到 Linux 配置。

## 3. 填写私有生产配置

只在首次创建时复制模板；已有私有配置不要覆盖：

```bash
cp -n .env.production.example .env.production
chmod 600 .env.production
```

使用编辑器填写 `.env.production`。内部数据库密码、会话、指标、嵌入认证等使用独立随机值；模板列出的内部密钥至少32位，使用字母数字、下划线和短横线。S3 凭证使用服务方提供的值。密钥不发聊天、不提交仓库。

主要配置：

| 字段 | 填写内容 |
| --- | --- |
| `PUBLIC_ORIGIN` | 实际访问来源，如 `https://agent.your-domain.com` |
| `PROD_DATA_ROOT` | 项目内独立相对目录，默认 `data/production` |
| `MODEL_PROVIDER / MODEL_ID / MODEL_BASE_URL / MODEL_API_KEY` | 实际模型协议、名称、HTTPS端点与密钥 |
| `STORAGE_ENDPOINT / S3_HOST / S3_PORT / S3_USE_SSL` | 同一个真实 S3 HTTPS 端点 |
| `STORAGE_BUCKET / MILVUS_STORAGE_BUCKET` | 不同的两个专用 Bucket |
| `STORAGE_ACCESS_KEY / STORAGE_SECRET_KEY` | S3 服务方凭证 |
| `BGE_MODEL_PATH` | 承载模型的机器上已有的缓存目录 |
| `COMPOSE_PROFILES` | 本地嵌入使用 `local-embedding`；远端嵌入则留空 |
| `EMBEDDING_BASE_URL` | 本地容器为 `http://embedding:8000/v1`；远端为实际兼容地址 |
| `TOOL_HTTP_ALLOWED_HOSTS / AMAP_API_KEY` | 使用天气路况时填域名JSON数组和高德Key |

S3 的 Region、寻址模式和私有 CA 按服务方填写，见 [S3接入说明](./S3_SETUP.md)。JSON数组使用英文双引号，例如 `TOOL_HTTP_ALLOWED_HOSTS=["restapi.amap.com"]`。

## 4. 放置证书与准备镜像

根据 `PROD_DATA_ROOT` 放置证书，默认文件位置为：

```text
data/production/tls/fullchain.pem
data/production/tls/privkey.pem
```

证书必须覆盖实际域名，私钥与证书匹配。自行管理证书时可使用 Let's Encrypt 的 ACME 工具申请和续期，见 [官方申请指南](https://letsencrypt.org/getting-started/)。本入口读取已有证书，不负责申请、续期或替换。首次启动会设置容器所需权限。

构建应用镜像，再拉取生产基础服务镜像：

```bash
backend/.venv/bin/python scripts/build-production-images.py
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml -p yugitalk-prod pull postgres redis etcd milvus prometheus jaeger
```

默认构建包含嵌入镜像；使用远端嵌入时，可用两个 `--component` 仅构建 backend 和 frontend。基础镜像已固定摘要，所需镜像必须在启动前存在；入口不隐式构建或拉取。

## 5. 预检与首次启动

先运行只读预检：

```bash
backend/.venv/bin/python scripts/start-production.py --env-file .env.production --project yugitalk-prod --check
```

预检核对凭证声明、存储配置、正式域名、证书/私钥、空持久目录、独立 Compose 项目、嵌入服务选项与已有镜像。不会创建业务目录、启动容器、访问 S3 或调用模型；通过不代表云存储与公网 HTTPS 已接通。

开始首次部署：

```bash
backend/.venv/bin/python scripts/start-production.py --env-file .env.production --project yugitalk-prod
```

执行顺序为：S3 两桶小对象写入/读取/删除探测 → 初始化配置 → 固定当前镜像 ID → 设置目录权限 → 迁移与服务启动 → 实际域名 HTTPS 和依赖健康检查。S3 探测会产生临时诊断对象，不覆盖业务对象，须事先给两个专用 Bucket 配置相应权限。

成功记录保存为 `data/production/first-deployment.json`，明确记录 `restore_drill=not_performed`、`backup_created=false` 和 `model_quality_verified=false`。服务启动与健康通过不等于真实模型问答、完整功能和安全审查全部通过。

此入口只用于全新部署；目标已有容器或持久数据时会拒绝。失败后保留服务和数据供排查，不自动删除、不回滚数据库。先查看错误阶段及容器状态；若启动已经写入数据库，用明确的修复流程处理，不能删数据来绕过首次部署检查。

## 6. 初始化账号与人工验收

首次管理员初始化交互输入密码：

```bash
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml -p yugitalk-prod exec api python -m app.identity.bootstrap --email your-email@example.com --name "管理员" --platform-admin
```

打开实际 HTTPS 域名，验收登录和刷新、上传及向量化流程、知识库检索、知识库无证据拒答、流式回答、停止任务、Tool配置与调用、文件删除。真实模型和工具调用会产生供应商用量，需要在自己的账户核对支出。

本轮按用户决定不做恢复演练，不填写 RPO/RTO 已达标；恢复、异机备份与正式质量/安全评测分别保留为后续事项。

## 7. 排障与停止

```bash
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml -p yugitalk-prod ps -a
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml -p yugitalk-prod logs --tail 100 migrate api worker scheduler frontend milvus
```

日志可能含第三方错误信息，分享前先脱敏。检查 DNS、证书、80/443入口、S3权限、模型密钥和嵌入路径。HTTPS检查失败后不会关闭证书验证继续报告成功。

停止使用同一组文件与项目名执行 `down`，不删除持久数据。后续升级使用 [部署说明](./DEPLOYMENT.md) 中的升级流程；其外部S3备份限制未被本入口移除。

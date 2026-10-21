# 开发端启动说明

以下命令适用于当前 Windows 项目 `E:\Code\YugiTalk`，在 PowerShell 中执行。选择一种方式即可：Docker 启动适合日常使用，本地热更新适合修改前后端代码。

## 更新顺序

先更新开发端，再更新生产端。代码与测试完成后，必须构建并更新实际运行的开发服务，验证页面、接口、权限及服务健康；开发端验证通过后，再按生产发布流程更新生产端。仅修改源码或生成截图不算完成开发端更新。

| 启动方式 | 页面 | API 文档 |
| --- | --- | --- |
| Docker 完整开发环境 | http://127.0.0.1:8080/ | http://127.0.0.1:18000/docs |
| 前后端本地热更新 | http://127.0.0.1:5173/ | http://127.0.0.1:8000/docs |

`https://localhost:18443` 是独立阶段六测试环境，使用自己的数据与账号。开发端使用根目录 `.env` 和项目 `data/`，不使用 `.cache/stage6-production.env`。

## Windows PowerShell 一键启动

在项目目录执行：

```powershell
.\start.ps1
```

启动分为镜像构建和服务启动两步，构建时显示完整依赖下载日志。首次下载可能持续数分钟；后端 pip 下载使用构建缓存，中断后重试可复用已下载内容。未改依赖时后续构建通常复用整个安装层。

此入口直接使用 Docker Compose，不依赖 WSL 或 Bash，已存在的 `.env` 和数据继续保留，启动结果与下方 `.sh` 一致。

PowerShell 中裸写 `bash` 可能调用系统 WSL；如果出现 `execvpe(/bin/bash) failed`，表示 WSL 缺少 Bash，尚未执行项目脚本。此时使用 `start.ps1`，或在已安装的 Git Bash 终端中运行 `bash start.sh`，无需为启动项目安装 WSL。

## Bash 一键启动

Linux、macOS、WSL 或 Git Bash 中，在项目目录执行：

```bash
bash start.sh
```

脚本自动定位项目目录，检查 Docker／Compose，初始化缺失的持久目录，校验配置，再构建并启动前端、API、迁移、Worker、Scheduler 及数据库等依赖，等待健康检查通过。使用已有 `.env`，不覆盖密钥与数据；未找到 `.env` 时仅复制模板并停止，须先填写配置后重新运行。不会创建默认账号。

打开 `http://127.0.0.1:8080/`，API 文档默认在 `http://127.0.0.1:18000/docs`，自定义端口以 `.env` 中的 `API_BIND_PORT` 为准。停止时在项目目录执行 `docker compose stop`，保留数据。

这是 Docker 完整开发环境启动入口，不启动 5173／8000 的本机热更新进程。启用本地嵌入时，先在 `.env` 配置 `COMPOSE_PROFILES=local-embedding` 和当前操作系统可访问的 `BGE_MODEL_PATH`，不会自动下载模型；远程 Embedding 不需启用该 profile。原 Windows 模型路径不能直接用于另一台 Linux 主机。

## 1. 准备配置

先打开 Docker Desktop，确认使用 Linux 容器，再进入项目：

```powershell
Set-Location E:\Code\YugiTalk
. ./scripts/set-project-cache.ps1
./scripts/init-project-data.ps1
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
```

已有 `.env` 继续使用，不覆盖模型密钥。首次配置时确认已有本地模型路径：

```dotenv
APP_ENV=development
COMPOSE_PROFILES=local-embedding
BGE_MODEL_PATH=E:/Code/HUGGINGFACE_CACHE/models--BAAI--bge-m3
EMBEDDING_PROVIDER=openai_compatible
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_DIMENSION=1024
```

默认使用已有 BGE-M3 缓存，不迁移或自动下载权重。使用本地嵌入服务时，`EMBEDDING_BASE_URL` 保持未设置，程序会分别使用正确的宿主机和容器地址。已经配置远程嵌入服务时按其地址运行，不启用 `local-embedding`。

聊天模型沿用你的私有 `.env`。使用 `MODEL_PROVIDER=mock`、`MODEL_ID=mock-v1` 可以离线演示；已有 DeepSeek 等真实模型配置会在发送任务时调用对应服务。启动服务本身不等于发送模型请求。

## 2. Docker 完整启动

在项目根目录执行：

```powershell
docker compose config --quiet
if ($LASTEXITCODE -ne 0) { throw '开发 Compose 配置检查失败' }
docker compose --profile local-embedding up --build -d --wait --wait-timeout 600
```

此命令构建当前代码并启动前端、API、迁移、Worker、Scheduler、数据库和本地嵌入服务。首次构建较慢；已有镜像也需要在代码更新后重建，避免仍运行旧版密码校验或 RAG。数据库迁移成功后 API 和 Worker 才会启动。

打开 **http://127.0.0.1:8080/**。

账号已存在时直接登录。没有账号时，在项目根目录初始化一次，将邮箱和名称换成自己的：

```powershell
docker compose exec api python -m app.identity.bootstrap --email you@example.com --name "你的名字" --platform-admin
```

密码由终端交互输入，不显示输入内容，不限制长度，但不能为空。没有默认登录密码；重复初始化不会修改已有账号密码。测试端账号不会自动出现在开发数据库中。

## 3. 前后端本地热更新

准备 Python 3.12、uv 和 Node.js ≥ 22.12。当前项目已提供 Python 运行时，下面使用其路径。

### 首次安装依赖

在项目根目录执行；依赖已安装且锁文件未变化时可以跳过：

```powershell
Set-Location E:\Code\YugiTalk
. ./scripts/set-project-cache.ps1
uv sync --project backend --frozen --python .runtimes/cpython-3.12.14-windows-x86_64-none/python.exe
if ($LASTEXITCODE -ne 0) { throw '后端依赖安装失败' }
npm.cmd --prefix frontend ci
if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败' }
```

下载缓存位于项目 `.cache/`。无需激活虚拟环境，后续命令直接使用项目 `.venv` 的 Python。

### 终端一：依赖与后端

```powershell
Set-Location E:\Code\YugiTalk
. ./scripts/set-project-cache.ps1
docker compose --profile local-embedding build api embedding
if ($LASTEXITCODE -ne 0) { throw '开发后端或嵌入镜像构建失败' }
docker compose --profile local-embedding up -d --wait --wait-timeout 600 postgres redis storage etcd milvus embedding migrate worker scheduler
if ($LASTEXITCODE -ne 0) { throw '开发依赖启动失败，请检查服务日志' }

Set-Location backend
.venv/Scripts/python.exe start.py --env development --check
if ($LASTEXITCODE -ne 0) { throw '本机后端配置检查失败' }
.venv/Scripts/python.exe -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw '数据库迁移失败，请先排查迁移日志' }
.venv/Scripts/python.exe start.py --env development --host 127.0.0.1 --port 8000 --reload
```

保持这个终端运行。Windows 的 Worker 使用 Docker；Scheduler 也已经在上述容器中运行。不要再对同一环境额外启动一组调度器。

本机 API 使用 `.env.example` 中的宿主端口：PostgreSQL 25432、Redis 26379、对象存储 29000、Milvus 29530；本地 Embedding 默认使用 18081。不要把容器内部的 `postgres`、`redis`、`embedding` 主机名写成本机 API 的连接地址。

### 终端二：前端

```powershell
Set-Location E:\Code\YugiTalk
. ./scripts/set-project-cache.ps1
Set-Location frontend
$env:API_PROXY_TARGET = 'http://127.0.0.1:8000'
npm.cmd run dev
```

打开 **http://127.0.0.1:5173/**。Vite 把 `/api` 和 `/health` 转发到本机 API，无需直接从页面访问 8000。确认 `.env` 的 `AUTH_TRUSTED_ORIGINS` 包含 `http://127.0.0.1:5173`，否则登录和写操作会返回 403。

没有账号时另开终端初始化本机后端连接的开发数据库：

```powershell
Set-Location E:\Code\YugiTalk\backend
.venv/Scripts/python.exe -m app.identity.bootstrap --email you@example.com --name "你的名字" --platform-admin
```

前后端源文件修改后自动更新；修改 Worker 使用的 RAG、任务代码或环境配置后，在项目根目录重新构建并重建对应容器：

```powershell
docker compose build api
if ($LASTEXITCODE -ne 0) { throw '后端镜像构建失败' }
docker compose up -d --force-recreate worker scheduler
```

本机 API 修改 `.env` 后也需要重启。本地热更新和 Docker 页面连接同一个开发数据库；两个 API 端口不同。

只修改模型密钥、工具密钥或域名白名单时，不需要构建镜像，但必须让 Docker 执行服务重新加载配置。在项目根目录执行：

```powershell
docker compose up -d --no-deps --force-recreate --wait --wait-timeout 90 api worker scheduler
```

若使用本机 8000 API，还需在原终端停止并重新启动它。`--reload` 主要监听 Python 文件，不保证重新加载 `.env`；`docker compose restart` 也不会更新容器环境变量。只重启本机 API 会出现“本机 Tool 页面就绪，但 Worker 仍报工具尚未配置”的不一致。

## 4. 检查与排障

在项目根目录查看状态及日志：

```powershell
docker compose ps -a
docker compose logs --tail 100 migrate api worker scheduler embedding
```

| 现象 | 检查方式 |
| --- | --- |
| 8080 打不开 | 检查 `frontend` 与 `api`；后端根地址不是聊天页面 |
| 5173 打不开 | 确认前端终端的 `npm run dev` 正在运行 |
| 本地页面提示网络失败 | 确认 8000 API 正在运行，检查 Vite 的代理目标 |
| `migrate` 退出码非零 | 查看迁移完整日志；修复后重新启动，不先跳过迁移 |
| Embedding 无法启动 | 检查 BGE 缓存路径及 `config.json`、权重文件；查看 embedding 日志 |
| 密码错误 | 使用开发库中的账号和密码；初始化已存在账号不会重设密码 |
| 工具尚未配置就绪 | 在 Tool 页面核对密钥与白名单，再重建 `api worker scheduler` 以加载 `.env`；本机 API 也需重启 |

本轮只核对启动入口、配置与命令，不自动启动开发栈或迁移开发数据。七步 RAG 的真实存储集成此前在 Milvus 2.5.27 隔离环境通过；当前开发配置锁定的 Milvus 2.5.14 仍需启动后单独核验。

## 5. 停止服务与缓存清理

本机前后端终端按 `Ctrl+C`。开发容器在项目根目录停止：

```powershell
docker compose down
```

业务数据仍保存在项目 `data/` 下，不要删除这些目录。独立阶段六测试环境使用不同 Compose 参数停止，见 [部署说明](./DEPLOYMENT.md#隔离验收和停止服务)。

阶段或依赖安装完成后可以定向检查 C 盘本项目依赖缓存：

```powershell
./scripts/clean-stage-cache.ps1 -Stage 6
./scripts/clean-project-images.ps1
```

清理保留虚拟环境、已有 BGE-M3 缓存、业务数据、现用镜像和回退资产。七步 RAG 的配置、旧索引升级与验收见 [实现与验收](./ENHANCED_RAG.md)。


## 数据集缓存位置

2026-10-19 将原 `C:\Users\xwChen\.cache\huggingface\datasets` 中的数据集语料迁入项目的 `.cache/huggingface/datasets/`（当前为 `E:\Code\YugiTalk\.cache\huggingface\datasets`）。C 盘旧路径保留目录联接，兼容历史缓存中的绝对路径；实际语料只存放在 E 盘，读取旧路径不会重新占用 C 盘存储。

`. ./scripts/set-project-cache.ps1` 会设置当前终端的 `HF_DATASETS_CACHE`；`backend/get_huggingface_data.py` 也显式使用项目缓存路径。迁移包含原始下载、解压结果及 Arrow 语料，逐文件校验大小和 SHA-256 后才清理 C 盘原件。不会改变模型权重缓存位置。

迁移校验清单位于 `.cache/huggingface/datasets-migration.json`。清理 C 盘时不要递归清空旧路径下的内容，它指向 E 盘语料；如需取消兼容路径，应只移除目录联接本身。

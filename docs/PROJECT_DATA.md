# 项目持久数据目录

2026-10-10：开发数据已从 Docker named volume 离线复制到项目 E 盘目录，主 Compose 与阶段 0 Compose 使用项目相对路径的 bind mount。BGE-M3 模型缓存按用户要求保持原路径。生产环境完整发布与备份恢复仍在阶段 6 验收。

## 数据位置

| 内容 | 项目目录 | 容器路径 |
| --- | --- | --- |
| PostgreSQL：账户、会话、任务、检查点、审计、资料分块等 | `data/postgres/` | `/var/lib/postgresql/data` |
| Redis：AOF、队列及运行状态 | `data/redis/` | `/data` |
| MinIO：上传原文 `yugitalk` bucket、Milvus 对象 `yugitalk-milvus` bucket | `data/minio/` | `/data` |
| etcd：Milvus 元数据 | `data/etcd/` | `/etcd` |
| Milvus：本地索引、WAL 和运行数据 | `data/milvus/` | `/var/lib/milvus` |
| 阶段 0 的 PostgreSQL／Redis | `data/stage0/postgres/`、`data/stage0/redis/` | 各测试服务数据路径 |
| 后续数据库与对象存储备份的默认目录 | `data/backups/` | 按备份命令指定 |
| uv、pip、npm 下载缓存 | `.cache/uv/`、`.cache/pip/`、`.cache/npm/` | 本机依赖工具使用 |
| BGE-M3 权重：保留原位置 | `E:/Code/HUGGINGFACE_CACHE/models--BAAI--bge-m3` | `/models`，只读 |

实际宿主根目录为 `E:/Code/YugiTalk`。阶段 2–5 使用开发基础服务内的随机数据库、专属队列和对象空间，持久文件最终仍落在上述项目目录。API 与 Scheduler 的临时文件不是持久业务数据；业务审计记录在 PostgreSQL。

向量持久化涉及 **Milvus、MinIO、etcd 三者**，不能只复制 `data/milvus`。Git 忽略整个 `data/`，根镜像构建上下文使用允许列表，不上传数据、备份或密钥。

Docker Desktop 的镜像层、构建缓存、容器 stdout 日志和引擎虚拟磁盘仍由 Docker 管理；本次迁移不修改系统级 Docker 存储位置。旧卷作为回退副本仍占用原 Docker 磁盘空间。

## 全新机器初始化

Windows，在项目根目录执行：

```powershell
./scripts/init-project-data.ps1
docker compose config --quiet
docker compose up --build -d --wait --wait-timeout 180
```

Linux，在项目根目录执行：

```bash
mkdir -p data/{postgres,redis,minio,etcd,milvus,backups} data/stage0/{postgres,redis} .cache/models
docker compose config --quiet
docker compose up --build -d --wait --wait-timeout 180
```

初始化只创建目录，不迁移旧数据，不下载模型，不覆盖已有文件。Compose 设置 `create_host_path: false`，目录缺失时明确失败，避免误挂载到自动创建的空目录。Linux 宿主机仍须确认磁盘容量与服务账号的读写权限；当前实际运行验证在 Windows Docker Desktop 完成，Linux CI 初始化已同步，未宣称完成服务器上线验收。

## 当前迁移与校验

本次迁移保留五个 `yugitalk-dev_*` 原卷及 `yugitalk-stage0_probe-db`。源卷只读，复制前停止应用与数据服务，逐文件 SHA-256 校验后才切换挂载；阶段 0 的旧数据库也已复制。未迁移任何其他项目的卷或 BGE-M3 缓存。

结果与校验指纹见：

- [迁移记录](./stages/artifacts/project-data-migration.json)：文件数量、大小、散列与迁移前后业务指纹。
- [挂载与重启检查](./stages/artifacts/project-data-check.json)：实际目录和重启前后数据一致性。

迁移记录中的 509 是 Milvus 集合统计记录数，与关系库中的 241 个分块不是同一种计数。原文校验覆盖 2 份可用资料，向量检索使用真实 BGE-M3。

本次实际验收结果：五个开发数据服务均为 E 盘项目目录挂载，10 个开发服务健康；数据库迁移版本保持 `0007_reliability`。停机重启后表记录数、原文散列、向量检索指纹、Redis 非过期业务数据指纹一致。阶段 0 的数据库与 Redis 从独立绑定目录启动健康，数据库可读取 4 张业务表，检查后已停止隔离服务。

迁移安全测试覆盖二进制文件与空目录保留、拒绝覆盖非空目标、拒绝源符号链接：Linux 容器 3 项通过；Windows 本机 2 项通过，符号链接项因本机权限跳过，已在 Linux 容器补验。沙箱内 Windows 临时目录权限导致的初次运行失败，已通过正常本机权限重跑并清理空临时目录。Ruff、格式检查、PowerShell 语法与 407 个 Python 声明的中文说明检查通过。C 盘本阶段依赖缓存定向检查完成，释放 0 MiB；没有新增下载依赖或删除模型、业务目录和旧数据卷。

查看当前挂载及业务状态：

```powershell
backend/.venv/Scripts/python.exe scripts/check-project-data.py
```

需要重新执行停机重启验收时，在没有进行中的任务且允许短暂停机的时段运行：

```powershell
backend/.venv/Scripts/python.exe scripts/check-project-data.py --restart
```

`scripts/migrate-project-data.py` 是一次性旧 named volume 迁移脚本，会检查原挂载和空目标；当前已经迁移，直接再次运行会拒绝执行，不能用于日常启动。

## 迁移回退

原 Compose 配置保存在迁移记录 `backup_directory` 指定的项目 `.cache/data-migration-*` 下。旧卷是迁移时刻的副本，**不包含迁移后新增的数据**。如已产生新数据，先停写并备份当前项目目录，制定同步方案，再恢复；不能直接回退覆盖新数据。

需要回退时，先停止 frontend、api、scheduler、worker，再停止 Milvus 和其他数据服务；保留当前 `data/` 与现有 Compose 副本，把备份目录的 `docker-compose.before.yml` 恢复到项目根目录，`docker-compose.stage0.before.yml` 恢复到 `deploy/archive/stages/docker-compose.stage0.yml`，然后执行 `docker compose config --quiet` 和 `docker compose up -d --wait`，最后校验数据。归档的阶段0配置需要使用 `--project-directory .` 验证。禁止同时运行新旧两套实例消费任务。

普通 `docker compose down` 不删除 bind mount 目录。不要手动编辑运行中的数据文件，也不要把运行中的目录直接复制当作一致性备份。正式 PostgreSQL、Milvus、etcd 与对象存储的一致性备份、保留策略和恢复演练将在阶段 6 实现。

阶段6新增明确例外：用户选择正式对象存储使用现有外部S3，上传原文和Milvus对象保存远端两个专用Bucket；数据库、队列、etcd、Milvus本地数据与监控仍绑定项目目录，BGE缓存保持原路径。开发MinIO与原数据保留。当前没有真实S3服务信息，本机冷备不能覆盖远端对象，真实跨存储恢复尚未验收。[配置与范围](./S3_SETUP.md)

## 人工验收

1. 打开工作台 `http://127.0.0.1:8080/`，确认原账户能登录、历史会话仍可查看。
2. 打开知识库，确认原文件与分块可用，用原资料问题检索，核对引用。
3. 在项目目录确认五个数据子目录有文件；运行挂载检查，确认所有业务持久挂载指向项目目录。
4. 查看重启检查报告，确认 `restart_verified=true`，前后指纹一致。
5. 确认 `.env` 的 BGE-M3 路径保持原值；旧卷仅保留作回退，本次没有删除它们。

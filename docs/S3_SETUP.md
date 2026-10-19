# 正式环境接入现有 S3

用户选择正式环境使用服务器或云端的现有 S3 服务。当前没有服务地址、Bucket 或凭证，**真实外部接入与恢复验收尚未执行**。开发数据保留；开发和隔离测试继续使用本机对象存储。正式默认配置不启动旧 MinIO。

## 准备信息

在私有 `.env.production` 填写下表，不向聊天、仓库或截图提供密钥。

| 配置 | 含义与示例 |
| --- | --- |
| `STORAGE_ENDPOINT` | 无路径、查询或嵌入凭证的 HTTPS 服务端点，如 `https://s3.example.com:443` |
| `S3_HOST` / `S3_PORT` / `S3_USE_SSL` | 与端点一致的主机、端口和 `true`；主机不带协议 |
| `STORAGE_BUCKET` | 预先创建的项目原文 Bucket，如 `yugitalk-documents` |
| `MILVUS_STORAGE_BUCKET` | 独立 Milvus Bucket，如 `yugitalk-vectors`，不能与原文相同 |
| `MILVUS_STORAGE_PREFIX` | 首次部署的对象前缀，默认 `yugitalk-milvus`；已有数据不能直接更改 |
| `STORAGE_REGION` | 服务方要求的 Region，默认 `us-east-1` |
| `STORAGE_ADDRESSING_STYLE` | 默认 `path`；服务方要求虚拟主机寻址时使用 `virtual` |
| `STORAGE_ACCESS_KEY` / `STORAGE_SECRET_KEY` | 服务方提供的项目专用凭证；支持常见云凭证的 `/`、`+`、`=`，不要求自行生成32位访问标识 |
| `S3_CA_SOURCE` / `STORAGE_CA_FILE` | 公共受信任证书时均留空；私有 CA 填宿主 PEM 路径及固定容器路径 `/etc/yugitalk/s3/ca.pem` |

私有 CA 由初始化脚本核对并复制到生产项目目录的 `s3-ca/ca.pem`，API、Worker、Scheduler 和 Milvus 只读挂载。正式初始化及后端运行时均要求HTTPS，始终验证证书；本机HTTP例外仅在固定localhost隔离覆盖文件显式启用。虚拟主机模式还须确认 Bucket 子域名可解析，证书覆盖对应域名。自定义端点是否完整兼容两个 SDK，需要在所选服务上验收。

## 权限和接通检查

运维预先创建两个 Bucket。原文应用需要 `ListBucket`、`GetObject`、`PutObject`、`DeleteObject`；Milvus 还需所用服务支持的分段上传和清理权限。凭证只授权这两个专用 Bucket，访问范围限定到部署所属对象前缀，不授予账户级建桶或修改策略权限。具体 IAM 策略按服务方文档制定，不能将通用动作清单当作已经验证的权限策略。

```powershell
backend/.venv/Scripts/python.exe scripts/check-production-s3.py --env-file .env.production
backend/.venv/Scripts/python.exe scripts/check-production-s3.py --env-file .env.production --write-probe
backend/.venv/Scripts/python.exe scripts/init-production.py --env-file .env.production
```

第一条只核对端点、两桶可访问与列表权限。第二条每桶写入一个小诊断对象，读取比对后删除并核对删除结果，不建桶、不覆盖业务对象；须在准备好专用 Bucket 后执行。清理失败会报告诊断对象键并返回失败，不能把它忽略为成功。报告不保存服务方异常正文、业务对象列表或凭证。Linux 使用 `backend/.venv/bin/python`。

版本化Bucket若返回本次上传VersionId，探测会删除这个确切版本，需要诊断前缀的 `DeleteObjectVersion` 权限。上传超时而无法取得版本号时，仅能尝试删除当前可见对象，可能存在不可见旧版本；按服务方的版本列表或生命周期规则复核诊断前缀，不把当前对象404称为历史版本全部清除。

初始化将 Milvus 的端口、SSL、Bucket、前缀、Region 与寻址方式明确写入 `config/milvus.yaml`，不依赖未声明的环境变量。已有配置内容变化时拒绝覆盖，需先完成变更和迁移计划。应用在正式环境遇到缺桶或拒绝访问会直接失败，不能自动申请更大权限。

## 数据和恢复范围

PostgreSQL、Redis、etcd、Milvus 本机数据与监控仍放在项目 `data/production/`。上传原文和 Milvus 对象改由远端 S3 持久保存，这是用户明确选择的项目目录约束例外；BGE-M3 缓存保持原路径。

**本机冷备脚本不能完整备份远端 S3。** 当前 `production-data.py backup` 和依赖它的发布脚本会在停机前拒绝外部 S3，避免生成不完整的成功快照。既有本机快照仅证明隔离环境恢复。真实服务明确后，需要配置并验证对象版本/快照备份，与停止写入后的 PostgreSQL、etcd 和 Milvus 恢复点对应；还要在独立 Bucket 和独立数据目录完成原文散列、向量检索及账户恢复。不能仅修改 Bucket 名称就宣称数据迁移完成。

## 本轮实际验证

- 配置边界及 SDK 单元测试通过，涵盖端点不一致、HTTP、相同 Bucket、私有 CA 配置缺失、Region 与寻址选项，以及正式环境禁止自动建桶。
- 本地 S3 协议替身的两桶读写删除检查通过，报告为 `stage6-s3-preflight.json`，明确 `scope=local_fixture`、`backup_restore_verified=false`。
- 更新后的隔离 HTTPS、真实 BGE-M3/Milvus 上传检索和监控验收通过；不证明云端网络、真实凭证权限或外部恢复已验证。

Milvus 配置键依据 [v2.5.27 官方配置](https://github.com/milvus-io/milvus/blob/v2.5.27/configs/milvus.yaml)；更多阶段状态见 [阶段6报告](./stages/06-production.md)。

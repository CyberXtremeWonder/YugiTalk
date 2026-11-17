# 数据盘点与过期身份记录清理

本批提供数据库只读盘点、清理预览，以及过期登录会话和邮箱验证码的小批量清理。默认不自动删除数据，不清理聊天、研究报告、知识库、审计或备份，也不改变现有每用户任务历史保留规则。

## 只读盘点

维护命令应在已有 API 容器内执行，复用该部署实际的 `DATABASE_URL`。不要把 `.env.production` 直接当作应用环境文件运行维护：模板中的数据库地址由 Compose 拼接，宿主机直接加载可能连接到开发环境。

使用与现有部署一致的 Compose 文件和项目名。下面是默认生产示例；如果已启用邮件告警，在命令中同时叠加 `-f docker-compose.alerts.yml`。新命令需要先随正常应用发布部署，本批开发没有重建或更新线上服务。

```bash
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml -p yugitalk-prod exec -T api python -m app.maintenance --dry-run
```

省略 `--dry-run` 仍然只读。输出为 JSON，只有聚合统计，不输出邮箱、令牌摘要、验证码摘要、用户标识、聊天正文或文件内容：

- `tables`：各应用表的记录数、最早时间及对应时间字段。PostgreSQL 的 `allocated_bytes` 包含表、索引及 TOAST 的已分配空间，不能当作可回收空间；SQLite 测试中为 `null`。
- `auth_cleanup_preview`：两类身份记录的总数、过期数、最早过期时间，以及当前单批上限下的候选数量 `next_batch`。
- `external_storage`：`measured=false`；`declared_document_bytes_not_deleted` 只是未标记 deleted 的文档在 SQL 中登记的原文大小，不是实际 S3 用量，不包含对象历史版本。
- `vector_store`：`measured=false`，没有扫描或修改 Milvus。研究文件目前位于数据库 JSON，其数据库空间计入研究报告表，而不是上述文档原文大小。

PostgreSQL 盘点使用 `REPEATABLE READ` 只读事务，结束时回滚，每条语句超时 5 秒。它仍需要执行各表聚合查询，应安排在低峰；大表超时会报告失败，不输出假统计或继续清理。表统计只在手动命令执行，不进入恢复调度器的高频循环。

## 手动清理一批

核对预览后，使用显式参数执行：

```bash
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml -p yugitalk-prod exec -T api python -m app.maintenance --apply-auth-cleanup --batch-size 500
```

规则固定为 `expires_at <= 本次UTC截止时间`。只处理 `login_sessions` 和 `email_verifications`；已消费但仍未过期的验证码也保留。每表默认最多 500 条，上限 1000；一次最多删除两个批次，而不是无限循环直到清空。CLI 不允许任意指定未来截止时间。

盘点和实际清理使用相同截止点；JSON 中的表统计与预览均来自清理前，`cleanup` 记录实际提交的删除数量。并发锁定或验证码重发可能使实际数量少于预览。大量历史积压可以手动多次执行，每次仍遵守批次上限。

PostgreSQL 在同一事务中选取候选并使用 `FOR UPDATE SKIP LOCKED`，避免等待正在使用的记录；真正删除时再次核对过期时间，保护刚重发的新验证码。两张表一起提交，任何一张表失败都会回滚本次两表删除。局部语句超时为 5 秒，锁等待超时为 1 秒。失败输出只含异常类别，不输出 SQL、参数或连接串。

登录会话过期字段已有索引，验证码过期字段目前没有单独索引；批量上限限制删除数量，不保证全表扫描或排序成本。规模增长时应另行验证并添加索引。删除后空间可能由 PostgreSQL 后续复用，不承诺宿主磁盘立即缩小。

## 可选定期清理

先发布代码并检查手动预览，再在对应私有环境文件中显式配置：

```dotenv
IDENTITY_CLEANUP_ENABLED=true
IDENTITY_CLEANUP_INTERVAL_SECONDS=86400
IDENTITY_CLEANUP_BATCH_SIZE=500
```

默认 `IDENTITY_CLEANUP_ENABLED=false`；间隔范围为 60～604800 秒，每表批量范围为 1～1000。仅 Scheduler 执行定期维护，API 和 Worker 不自动触发清理。配置通过基础 Compose 环境传入，生产合并配置继承这些字段。

```bash
docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml -p yugitalk-prod up -d --no-deps --force-recreate --wait scheduler
```

启用后在 Scheduler 首次恢复扫描后执行一批，以后从该批完成时按间隔再次执行。重启会重新计算计划；多个 Scheduler 也可能各自执行，数据库跳锁和过期条件保证不会重复删除同一条有效记录，但不是全局“每自然日只有一批”。日常清理不做全库盘点，历史积压不保证一天清空。

成功日志仅记录两表删除数量与截止时间；数据库失败单独记录异常类别和代码位置，保持恢复调度继续运行，下个维护周期重试。当前邮件规则未单独覆盖此维护失败。将开关改为 `false` 并重建 Scheduler 即可停用后续自动批次。

## 回归边界

自动测试覆盖默认只读、无自动提交、有效记录保护、过期边界、每表限批、重复执行、两表失败回滚、验证码刷新、调度开关及日志脱敏。SQLite 的测试不代替 PostgreSQL 锁行为；独立 PostgreSQL 验证结果另行记录，不以 SQL 编译通过冒充并发实测。

聊天和研究报告的删除语义、对象存储旧版本、备份淘汰和密钥轮换仍是后续独立事项，本功能不会替用户决定它们的保留期限。

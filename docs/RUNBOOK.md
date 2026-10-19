# 开发环境运行与阶段 5 恢复排障

生产部署与数据操作已实现，见 [部署说明](./DEPLOYMENT.md)；[阶段6](./stages/06-production.md)仍有未通过门槛，不代表生产上线验收通过。

## 任务停留在排队或处理中

```powershell
docker compose ps
docker compose logs --tail 80 scheduler worker
```

排队意图保存于 PostgreSQL。Redis 恢复后 Scheduler 补投，默认间隔 5 秒；同任务投递至少间隔 10 秒。处理中任务只有租约到期后才可回收；默认 45 秒，健康 Worker 会续租。不要通过 SQL 修改运行状态或参数，避免绕过权限、预算和租约令牌。

达到恢复次数上限后会进入失败状态并释放预算。可恢复的模型错误在任务记录页显示“确认恢复任务”，恢复可能再次调用模型并计费。权限撤销、工具下线和参数变化不能直接重试。

## 旧工单功能下线

创建工单及配套界面和接口已移除。旧助手配置中的 create_ticket 在使用时过滤；数据库版本和历史数据保留。旧 waiting_approval 任务由 Scheduler 以 tool_removed 结束、关闭待审批记录并释放预算，不能确认恢复。相关业务扩展等全部阶段完成后再实施。

## 文档处理中发生宕机

Scheduler 回收过期文档租约，增加索引版本并从原文重新导入；旧 Worker 无法使旧版本重新可检索。失败原因“导入 Worker 多次中断”时，先检查 Worker 和嵌入服务，再从知识库页重试。清理失败的已删除文档继续保持不可检索，页面提供重试清理。

## 隔离故障演练与缓存

```powershell
pwsh -NoProfile -File scripts/test-stage5.ps1 -IncludeContainers -IncludeUI
pwsh -NoProfile -File scripts/clean-stage-cache.ps1 -Stage 5
```

故障脚本只允许随机命名的阶段 5 数据库和 `yugitalk-stage5-test` 容器，强制终止该测试 Worker；不会终止开发 Worker。缓存清理不删除模型、虚拟环境或数据卷。手动安装前执行 `. ./scripts/set-project-cache.ps1`。

## 生产诊断边界

使用部署说明中的两份Compose与生产环境文件，禁止复用开发项目名。只有进程存活但ready=503时，检查内部依赖；首次迁移失败先确认PostgreSQL已经监听TCP。生产不暴露调试文档和指标公网入口，指标使用独立认证。定位任务使用服务端request_id、run_id、trace_id，不记录原始提示词或密钥。

冷备、恢复、发布应串行运行，选择维护窗口；发现镜像严重漏洞或未处置高危记录时不得直接发布。每日异机备份与外部通知渠道尚未配置，不宣称已满足主机丢失恢复目标。

正式对象存储改为现有S3后，不要用本机MinIO目录替代远端对象备份。当前本机备份/发布脚本在停机前拒绝外部S3，需先补齐对象版本/快照和独立恢复演练。S3未接通时按 [配置说明](./S3_SETUP.md)核对端点、两桶、Region、寻址及私有CA；不要扩大应用权限或关闭证书验证来绕过错误。

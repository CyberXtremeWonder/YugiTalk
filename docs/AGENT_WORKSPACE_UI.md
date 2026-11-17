# Agent 配置工作台

2026-11-02 按确认的蓝白效果图改为三栏：助手列表、分区编辑、配置摘要与发布。

- 初次加载选中第一位已发布助手；支持助手名称搜索和新建草稿。
- 基础配置包含名称、模型连接、角色指令与规划检查开关。
- 工具权限与运行限制分区展示；所有字段继续遵循原服务端契约。
- 配置摘要跟随草稿变化，发布成功后重新读取并选中最新版本。
- 高级参数默认隐藏；提交时若隐藏分区存在无效字段，自动切换并聚焦该字段。
- 密钥不回显；仅连接目标未变时允许留空保留，改变 URL 后要求重新填写。
- 新建入口恢复至助手目录，删除恢复至当前助手编辑区并沿用确认弹窗；删除当前助手会重置配置。
- 原有版本冻结及历史任务保留语义不变；支持深色主题和窄屏布局。

## 验证与容器更新

70 项前端测试及 13 项后端连接、密钥和删除测试通过；TypeScript、ESLint、生产构建通过。
后端连接测试使用足额隔离预算，供应商切换测试提高隔离空间额度，以免被已有工具 Schema
的保守预留提前拦截；未修改运行时预算或后端逻辑。

`node scripts/check-agent-workspace-ui.mjs` 使用隔离 API 数据验证搜索、页面内编辑、密钥保留、
连接目标变更校验、跨分区字段校验、工具和限制参数、版本发布、新建与删除、主题和窄屏。
截图保存在 `frontend/.cache/agent-workspace-ui/`，均为验收数据。

本次使用运行项目的 Compose 配置重建并更新 frontend：

```powershell
docker compose -p yugitalk-dev -f docker-compose.yml -f docker-compose.gpu.yml build frontend
docker compose -p yugitalk-dev -f docker-compose.yml -f docker-compose.gpu.yml up -d --no-deps frontend
$env:UI_BASE_URL='http://127.0.0.1:8080'
node scripts/check-agent-workspace-ui.mjs
```

更新后 `yugitalk-dev-frontend-1` 为 healthy，首页 HTTP 200。
实际提供 `/assets/index-DAgu5Cb7.js`，已核对包含新版工作台。
针对容器提供的资源再次执行浏览器验收通过，业务 API 保持隔离，没有修改真实助手。
后端、Worker、Scheduler 和数据库没有随本次前端更新重启。

后续按用户要求恢复新建、删除入口，最新浏览器验收和容器资源见 [TASK_HISTORY_UI.md](TASK_HISTORY_UI.md)。

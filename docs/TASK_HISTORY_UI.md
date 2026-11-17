# 任务记录工作台与原有操作入口

2026-11-02 按确认的效果图改造任务记录页，并按后续要求恢复改版前的新建、删除入口。

## 任务记录

- 顶部显示当前记录的全部、运行中、已完成、需处理数量。排队任务计入运行中，失败及等待补充/审批任务计入需处理。
- 列表按创建时间倒序分页，返回最近 100 条记录及全部活动任务；旧暂停或待补充任务仍可访问。支持状态筛选、标题/内容/任务 ID 搜索。
- 点击记录在右侧查看摘要、状态、时间、模型、执行次数、技能版本和 Token 用量；打开对话沿用原导航。
- `attempts` 表示执行次数，未当作人工恢复次数。输入、输出缺失时显示“未提供”，与实际零值区分；保守额度单独显示。
- 恢复入口严格由服务端 `retryable` 决定，确认后沿用 `/runs/{id}/retry` 与 CSRF 校验。
- 网络结果不确定时复用同一幂等键，成功后清除。等待响应时禁用重复提交和关闭；取消确认不发送请求。
- 列表刷新失败保留已加载内容并显示错误；支持空列表、无匹配结果、深色主题和手机宽度。

## 新建与删除

仅恢复改版涉及的操作入口，保留蓝白布局及原接口、权限、软删除和不可变版本规则：

| 页面   | 新建入口                                      | 删除入口和原有确认方式                           |
| ------ | --------------------------------------------- | ------------------------------------------------ |
| Agent  | 助手目录的“＋ 新助手”                         | 当前助手编辑区直接删除，沿用确认弹窗             |
| Skill  | 技能目录的“＋ 新 Skill”                       | 当前技能编辑区直接删除，沿用原直接操作           |
| MCP    | 配置区的“＋ 添加 MCP 服务器”                  | 服务器列表直接删除，沿用确认弹窗                 |
| Tool   | 工具目录的提交审核入口；管理员另有“＋ 新工具” | 可维护的 HTTP 工具编辑区直接删除，沿用原直接操作 |
| 知识库 | 名称输入和“创建知识库”内嵌显示                | 知识库直接删除并确认，文件行保留直接删除         |

移除了上述资源为删除增加的三点菜单。内置工具和无维护权限的资源不提供删除；普通成员仍通过审核提交查询工具。
对话原有新建入口、三点菜单、删除确认及 PDF/Word/TXT/JSON 导出保持原样。
其他未改版模块的原有操作流程没有改动。

## 验证与部署

前端 17 个文件、70 项测试通过；修改文件的 ESLint、TypeScript 和容器生产构建通过。
后端任务相关测试 7 项通过：

```powershell
cd backend
$env:OTEL_EXPORTER_OTLP_ENDPOINT=''
.\.venv\Scripts\python.exe -m pytest tests/test_task_retention.py tests/test_reliability.py::test_retry_new_key_and_incremental_budget -q -p no:cacheprovider --tb=short --show-capture=no
```

浏览器脚本 `check-task-history-ui.mjs` 验证分页、倒序、统计、筛选、搜索、空值/零值、待补充提示、
恢复权限、取消和等待响应、幂等键、打开对话、刷新保持选中、复制 ID、空列表和错误恢复、深浅色与手机布局。
Agent、Skill/MCP/Tool、知识库三个验收脚本同时检查恢复后的新建和删除操作。
这四组脚本均在开发服务及更新后的容器资源上通过；所有 API 为隔离数据，没有操作真实资源或恢复真实任务。
对话菜单脚本也通过，新建/删除/重命名及四种导出保持正常。

```powershell
docker compose -p yugitalk-dev -f docker-compose.yml -f docker-compose.gpu.yml build frontend
docker compose -p yugitalk-dev -f docker-compose.yml -f docker-compose.gpu.yml up -d --no-deps frontend
$env:UI_BASE_URL='http://127.0.0.1:8080'
node scripts/check-task-history-ui.mjs
node scripts/check-agent-workspace-ui.mjs
node scripts/check-configuration-workspaces-ui.mjs
node scripts/check-knowledge-library-ui.mjs
```

前端容器 healthy，首页及实际 JS 均为 HTTP 200。实际资源 `/assets/index-Cvq9PldW.js`、
`/assets/index-CkWyb4IJ.css`，镜像
`sha256:506bd22bbfd08d43e49b270d610cb92543db4cb05e5f621cd44ab9f27b500ab2`。
仅更新前端，其余服务检查时均 healthy。本次没有修改后端代码。

截图为示例数据，输出到 `frontend/.cache/task-history-ui/`、`agent-workspace-ui/`、
`configuration-workspaces-ui/`、`knowledge-library-ui/`。
任务记录入口：`http://127.0.0.1:8080/#tasks`。

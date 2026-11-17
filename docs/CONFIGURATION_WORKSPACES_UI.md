# Skill、MCP、Tool 配置工作台

2026-11-02 按确认的蓝白设计更新三个页面，与 Agent、知识库共用主题。

- Skill：技能搜索和列表、工作流程编辑、工具范围、版本摘要和发布；保留只读权限、立即启停和删除。
- MCP：服务器列表、连接配置、凭证、已发现工具和连接摘要；保存与测试分开操作。
- Tool：内置、HTTP、MCP 来源筛选与搜索，目录开关和详情面板；HTTP 配置、参数和校验分区展示。
- 保留服务端权限及不可变版本契约。内置工具不能覆盖或删除；MCP 工具连接跳转至 MCP 页面；普通成员通过提交审核新增 HTTP 工具。
- MCP Token 只写入不回显，空白保留、明确勾选才清除；修改配置或凭证后先保存，再发现或授权工具。
- Tool 参数校验针对已发布版本，不执行外部请求；隐藏分区的无效字段在提交时自动展开并聚焦。
- 三个页面均支持浅色、深色和窄屏布局；后续按用户要求恢复原有新建、直接删除入口和确认方式。

## 验证

前端 17 个测试文件、70 项测试全部通过；TypeScript、修改文件的 ESLint、生产构建通过。
浏览器验收脚本 `scripts/check-configuration-workspaces-ui.mjs` 同时检查：

- Skill 详情读取、只读权限、版本发布、工具范围、新建、启停和删除。
- MCP 凭证保留/清除/替换、未保存禁止测试、连接失败反馈、发现工具、启停、删除确认和 SSE 新建。
- Tool 三种来源、内置只读、管理权限、参数编辑、隐藏字段校验、发布、已发布参数校验、启停和成员提交审核。
- 深浅色主题、手机宽度无页面横向溢出、无未处理页面异常。

脚本先在开发服务执行，再通过 `UI_BASE_URL=http://127.0.0.1:8080` 对容器资源执行，均通过。
Agent 原有浏览器验收也针对更新后的容器通过。浏览器验收使用隔离 API 数据，不更改真实用户配置，
也不代表真实 MCP 远端服务已连通。截图输出在 `frontend/.cache/configuration-workspaces-ui/`。

补充后端回归命令：

```powershell
cd backend
$env:OTEL_EXPORTER_OTLP_ENDPOINT=''
.\.venv\Scripts\python.exe -m pytest tests/test_skills.py tests/test_tools_management.py tests/test_mcp.py -q -p no:cacheprovider --tb=short --show-capture=no
```

结果为 73 项通过、3 项失败，均在 `test_skills.py` 的执行测试中。本次仅更新前端，没有修改这些测试或后端运行逻辑。
这些失败仍待处理，未计入通过结果：

| 测试                                                | 预期                  | 实际                 |
| --------------------------------------------------- | --------------------- | -------------------- |
| `test_frozen_version_prompt_and_idempotency`        | 任务成功              | 任务失败             |
| `test_tool_intersection_blocks_forged_call`         | `tool_not_authorized` | `execution_error`    |
| `test_context_limit_does_not_truncate_instructions` | `context_limit`       | `model_empty_result` |

## 容器更新

```powershell
docker compose -p yugitalk-dev -f docker-compose.yml -f docker-compose.gpu.yml build frontend
docker compose -p yugitalk-dev -f docker-compose.yml -f docker-compose.gpu.yml up -d --no-deps frontend
$env:UI_BASE_URL='http://127.0.0.1:8080'
node scripts/check-configuration-workspaces-ui.mjs
node scripts/check-agent-workspace-ui.mjs
```

`yugitalk-dev-frontend-1` 已更新且 healthy，首页和 JS 均为 HTTP 200。
核验实际资源 `/assets/index-BUgnESJh.js` 包含三个新版页面，镜像为
`sha256:5966037f38f82d0b77169d4acd9a8f9b7513873f5ece9709ce0dc8007c3402ec`。
仅重建前端容器；其余现有服务检查时均 healthy。

入口：`http://127.0.0.1:8080/#skills`、`/#mcp`、`/#tools`。

最新入口调整、浏览器验收和容器资源见 [TASK_HISTORY_UI.md](TASK_HISTORY_UI.md)。

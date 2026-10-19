# Tool 管理、默认网络查询与验收

本轮参照本地 `E:\Code\SuperMew-main` 的 `0017-skill-tool-registry.md`、工具管理运行手册和安全 HTTP 执行思路，接入 YugiTalk 的任务快照、租约和技能授权。自定义工具支持声明式只读 HTTPS GET JSON，不开放脚本、Shell、SQL、任意 POST、写入工具或远程 MCP。固定内置 `web_search` 使用 Tavily 的 POST 搜索协议，属于只读查询，不能更换端点。

## 1. 页面与默认工具

2026-10-12新增个人工具提交审核：普通用户提交后须由开发模式平台管理员批准，批准后仍绑定提交者；同空间其他用户不可见、不可用，普通用户不可绕过审核直接创建或发布版本。本人可维护自己的个人工具，管理员空间工具和内置工具保持原范围。最新操作见 [身份与审核说明](./REGISTRATION_AND_TOOL_REVIEW.md)，下文的空间管理权限需同时满足该所有者与审核限制。

登录后点击左侧 **Tool**。默认目录包含文本统计、报告排版、天气查询、路况查询、网页搜索；内置工具只能启停，不能覆盖实现或删除。空间所有者和管理员可以创建、发布版本和删除自定义工具；普通成员只能查看角色允许的目录。

“已启用”表示空间允许使用，“待配置”表示服务配置尚未就绪，两者是不同状态。待配置工具不会发送给模型，也不会返回伪造的查询结果。

天气对接高德实况/预报接口，使用 `city` 六位行政编码和可选 `extensions=base/all`，时间以接口 `reporttime` 为准。[官方天气接口](https://lbs.amap.com/api/webservice/guide/api/weatherinfo)

路况对接高德道路交通态势接口，输入道路名 `name` 和六位市级编码 `adcode`；这是道路状态查询，不包含导航路径、交通事故保证或预计到达时间。[官方路况接口](https://lbs.amap.com/api/webservice/guide/api/trafficstatus/)

## 2. 配置真实查询

只在项目私有 `.env` 中填写，不要将真实 Key 提交到仓库、技能正文或聊天。需要高德 **Web 服务** 类型 Key，并确认账号已开通所需接口权限；路况接口的实际授权和服务可用性仍需自己的账号验证。

```dotenv
TOOL_HTTP_ALLOWED_HOSTS=["restapi.amap.com"]
AMAP_API_KEY=填写你自己的服务端Key
TOOL_SECRETS={}
```

若已有域名白名单，在数组中追加高德域名，保留现有项。Key 仅由服务端执行层注入固定请求的 `key` 查询参数，不写入任务快照、工具参数、版本记录或公开事件。自定义工具的命名密钥使用 `TOOL_SECRETS` JSON 映射，执行时采用 Bearer；不支持用户自定义密钥请求头或查询串。

数组和对象必须使用 JSON 格式：内部字符串用英文双引号，不能写成 `['restapi.amap.com']`。修改后可先在 `backend` 目录执行 `.venv/Scripts/python.exe start.py --env development --check`；格式错误会指出字段名，不输出密钥原值。

Docker 开发环境从项目根目录执行：

```powershell
docker compose up -d --no-deps --force-recreate api worker scheduler
```

本地后端在重新加载环境后重启；配置字段同样适用于 Linux 服务器及 `.env.production`，生产采用 `docker-compose.yml` 叠加 `docker-compose.prod.yml`。本轮只核验生产模板配置传递，没有部署正式服务器。

重新打开 Tool 页面，天气、路况应不再显示“待配置”。已有助手的历史授权不会自动增加新工具：在 **Agent** 页勾选天气和路况并发布新版本；若选用 Skill，也须在该 Skill 中授权相同工具。回到对话选择新助手版本。测试实时工具时先不选知识库，以免知识库无证据拒答规则提前结束任务。

示例问题：

- 查询北京市东城区天气，城市编码 110101。
- 查询深圳市北环大道当前路况，市级编码 440300。

需要配置真实模型支持工具调用；Mock 仅演示文本工具，不假装完成真实天气或路况查询。真实查询可能消耗模型及高德服务额度，默认自动验收不发出这些请求。

### 2.1 Tavily 网页搜索（2026-10-18）

项目私有 `.env` 已增加空的 `TAVILY_API_KEY=`，域名白名单已追加 `api.tavily.com`，原白名单项保留。填写自己的 Key 后重启 API、Worker 和 Scheduler；模板及开发／生产 Compose 均传递该字段。密钥仅在执行层以 Bearer 发送给固定 `https://api.tavily.com/search`，不进入工具 Schema、任务快照、账本结果或公开事件。

```dotenv
TAVILY_API_KEY=填写你自己的TavilyKey
TOOL_HTTP_ALLOWED_HOSTS=["restapi.amap.com","api.tavily.com"]
```

新建 Agent 默认勾选 `web_search`；旧版本不会自动获得新权限，需勾选“网页搜索”并发布新版本。选用 Skill 时也须授权同一工具。未配置密钥、白名单或工具被停用时，不会把该工具交给模型。

输入为 `query`（1–500 字符，禁止空白）与可选 `max_results`（1–5，默认 3）。服务端固定 basic 搜索、general 主题，获取正文但不获取供应商生成答案或图片。返回来源标题、网址、摘要及正文摘录；可把目标网址放入搜索词，但这不保证精确读取该 URL，不提供浏览器交互、登录或任意直连抓取。协议依据：[Tavily Search 官方文档](https://docs.tavily.com/documentation/api-reference/endpoint/search)。

网络仍执行白名单、公网 DNS 固定、TLS 校验、取消、20 秒截止时间及禁止重定向。Tavily 原始响应最多 2 MiB，进入模型的规范结果最多 16000 字节。完整正文仍超限时，同一次工具调用最多追加一次不含正文的搜索请求，沿用原 20 秒截止时间；成功时标记 `content_mode=snippets` 和 `raw_content_omitted=true`，模型不得把摘要描述为完整页面。该降级可能产生额外一次 Tavily 服务计费；每项摘要最多 700 字节、正文摘录最多 1300 字节。没有命中时返回成功的空列表，鉴权失败、限流、格式错误或响应过大返回明确工具错误，不伪造来源。网页正文属于不可信资料，不能授予权限。

搜索会把查询词发送到 Tavily，并可能消耗 Tavily 服务额度；默认测试只使用合成密钥与离线响应。Mock 不会假装执行真实网页搜索。选择知识库后仍遵守知识库证据约束，空检索会先拒答，不借网页搜索绕过该约束。

代码在 `backend/app/tools/web_search.py`，网络执行沿用 `http_runtime.py`，专项回归在 `backend/tests/test_web_search.py`。真实 Key、外部网络、搜索覆盖率和真实模型调用质量仍需独立联调。

## 3. 自定义查询

点击“新工具”，填写稳定标识、中文名称、用途、固定 HTTPS 地址、标量查询参数、空间角色与可选密钥引用。

```json
[{"name":"q","type":"string","description":"查询关键词","required":true}]
```

不允许 URL 凭证、预置查询串、私网地址、任意端口和远端 Schema 引用。参数最多 12 项，类型限 string/integer/number/boolean；工具超时 1–20 秒，JSON 响应 256–16000 字节。服务端还必须显式配置域名白名单；注册工具不会自动扩展白名单。

“校验参数”只校验已发布版本的示例输入，不调用网络、不调用付费模型。未保存的编辑内容不影响该按钮使用的版本。

修改后发布 v2，已有任务继续使用冻结的 v1 端点。启停和删除属于实时策略：删除后目录立即隐藏，历史版本和审计保留；已有任务不能继续或恢复已撤销的工具。

## 4. 调用链与安全边界

1. Agent 和 Skill 发布时检查工具是否属于当前空间；Skill 只能收窄授权。
2. 创建任务时取 Agent 授权、可选 Skill 授权、角色策略、启用状态和配置就绪的交集，冻结工具版本、Schema 和内容哈希。
3. 模型请求只携带冻结后的 Schema；模型提案不能自行扩充目录。
4. 每次执行、恢复、公开增量及成功账本复用前重新检查成员身份、启停、版本哈希与当前角色收窄策略。
5. HTTP 层严格校验参数，检查实际 DNS 解析；任何非公网结果都会拒绝，将已校验 IP 固定到连接，TLS SNI/证书和 Host 仍使用原域名。禁止代理、重定向及关闭 TLS 校验。
6. 网络请求期间不持有任务行锁，支持取消和续租；返回结果提交前再次检查租约与权限。响应限制格式、字节数与期限，并拒绝密钥回显。
7. 工具输出仍是不可信内容，不能获得系统权限；固定安全策略要求实时信息依据成功查询，失败时明确说明。提示词规则降低误用风险，但不能保证真实模型完全抗注入。

成功结果落入 `tool_executions`，用于重试复用；只读请求在发出后宕机、结果尚未提交时可能再次执行 GET，不能宣称网络请求“恰好一次”。DNS 系统解析无法强行取消，采用四个共享槽位限制残留解析并发；超时不产生无界排队。

## 5. 代码与存储

| 入口 | 职责 |
| --- | --- |
| `backend/app/tools/router.py` | 空间目录、管理员 CRUD、本地参数校验 |
| `management_contracts.py` | 声明契约和端点格式限制 |
| `models.py`、迁移 `0010_tool_management` | `managed_tools` 启停目录、`tool_versions` 不可变配置 |
| `service.py` | 就绪检查、快照、哈希、当前策略与恢复授权 |
| `default_http.py`、`registry.py` | 默认工具与固定高德查询契约 |
| `http_runtime.py` | 公网 DNS 固定、TLS、密钥注入、只读执行与响应限制 |
| `tasks/execution.py`、`tasks/reliability.py` | 租约、取消、账本复用和提交前复核 |
| `frontend/src/tools/` | 管理页面及 Agent/Skill 共用授权选项 |

配置及版本保存在项目 PostgreSQL，任务快照保存在 `runs`，结果保存在 `tool_executions`；开发数据位于项目 `data/postgres`。真实密钥只在私有环境配置，不存版本数据库。不安装新的第三方依赖，类、方法和具名函数均附中文说明。

## 6. 自动与人工验收

```powershell
$env:PYTHONUTF8='1'
backend/.venv/Scripts/python.exe -m pytest -c backend/pyproject.toml backend/tests/test_tools_management.py -q
npm --prefix frontend run build
node scripts/check-tools-ui.cjs
backend/.venv/Scripts/python.exe scripts/check-tools-compose.py
```

浏览器脚本使用真实 FastAPI、隔离内存数据库与离线模型，结束后不留下业务数据。自动测试检查声明式工具、安全网络边界、密钥注入、版本冻结、缓存撤销及实际执行器提交。证据位于 `docs/stages/artifacts/tools-*.json`、`tools-backend-tests.xml` 与页面截图。

人工验收顺序：

1. 刷新 `http://127.0.0.1:8080/#tools`，确认五项默认工具与“待配置”状态。
2. 创建一个只读查询声明，发布 v2，检查 Agent 和 Skill 能勾选它；参数校验不产生外部请求。
3. 停用再启用，删除自定义工具后目录立即隐藏；内置项没有编辑/删除入口。
4. 配置自己的 Key、白名单及真实模型，发布助手授权，在对话中分别查询天气和路况，确认流程显示工具名称与服务数据。
5. 未配置、无权限、供应商拒绝、超时或无数据时不显示伪造成功数据；测试时使用自己的许可接口和测试额度。

本轮自动验证不代表真实高德账号权限、服务器网络、供应商费用或真实模型输出质量已验收。数据库迁移前保留完整 PG 归档并核对原表行数，归档目录可读取不等于完整恢复演练；旧镜像不能独立作为新增工具版本数据的回滚保证。

最终验证（2026-10-11）：后端 **288 通过、3 跳过**，其中工具专项 **39 通过**；前端单测 **35 通过**。真实 API 与隔离 SQLite 的浏览器 CRUD/授权/390px 布局通过，开发及生产模板配置传递通过，Ruff、Mypy、格式和自有中文说明检查通过。开发 PostgreSQL 已迁移到 `0010_tool_management`，原业务表行数一致；原消息、知识库和技能没有被测试脚本修改。

本机健康验收显式绕过系统代理直连回环地址：默认代理曾让本地探测返回 502，直连的实际服务状态为健康。真实工具请求也不使用系统代理，并保留证书校验。

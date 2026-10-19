# 阶段 1：工程骨架与开发部署验收

日期：2026-10-09。前置：用户已验收阶段 0。本阶段状态：**用户已确认“阶段 1 验收通过”。** 以下保留本阶段交付时的测试证据与功能边界。

## 已交付

- React + TypeScript + Vite 应用，默认浅色、左上角品牌、左侧导航、独立页面；点击输入框无黑边，键盘导航保留橙色提示。抽屉使用 Radix 管理焦点、Escape 和焦点恢复。
- FastAPI 应用工厂、统一启动文件 `backend/start.py`、开发／测试／生产配置校验、JSON 日志、服务端请求 ID、统一脱敏错误响应。
- `/health/live` 独立检查进程存活；`/health/ready` 实际探测 PostgreSQL 迁移状态、Milvus REST 业务接口、Redis、MinIO，任何依赖不可用返回 503。
- 非 root API／Worker／前端容器、锁定镜像摘要、锁文件依赖导出；Alembic 独立迁移管理关系数据库，当前版本 `0002_milvus`，API 启动不隐式执行迁移。
- `docker-compose.yml` 部署开发前端、API、Worker、PostgreSQL、Milvus、etcd、Redis、对象存储和迁移服务。所有宿主端口仅绑定本机，数据卷独立于阶段 0。
- `.env.example`、测试复跑脚本、GitHub Actions CI 配置、根目录 README 和更新的项目说明书。

## 实测结果

| 检查 | 结果与证据 |
| --- | --- |
| Python 测试 | 40 通过、3 跳过；跳过项为阶段 0 的显式容器集成测试，本阶段真实基础设施另行验证；见 `artifacts/stage1-backend-tests.xml` |
| 后端规范与严格类型 | Ruff 检查、格式检查及 21 个自有模块的 mypy 通过 |
| 配置边界 | 开发无模型密钥可预检；生产缺少真实模型／独立凭证失败；生产禁止 DEBUG、热重载、通配 CORS、无认证 Redis；生产关闭文档接口 |
| 前端检查 | ESLint、TypeScript、Prettier、Vite 构建通过；Vitest 2 项接口边界测试通过；npm 安装审计 0 漏洞 |
| 浏览器 | 5 项通过；三种视口、深浅主题、无整体溢出、输入无黑边、独立页面、清空草稿、抽屉焦点恢复、真实同源 API；axe WCAG A/AA 检查无违规 |
| 开发服务启动 | 初次阶段 1 创建三个卷，Milvus 切换新增 etcd／Milvus 卷；完整构建部署成功，八个常驻服务健康，迁移退出码 0 |
| 真实队列 | API 发出基础设施 ping，Linux Worker 执行并返回真实 Redis 结果 |
| 故障与恢复 | Redis 停止：ready=503、live=200；启动 Redis：ready=200；API 接收退出信号后输出完成关闭日志，重新启动恢复就绪 |
| 迁移 | 离线 SQL、重复执行和普通 PostgreSQL 空库迁移通过；镜像切换保留数据卷，版本为 0002_milvus，旧向量扩展数量为 0 |

浏览器结果见 `artifacts/stage1-ui-results.json`，基础设施结果见 `artifacts/stage1-infrastructure.json`。当前截图：`artifacts/stage1-light-360.png`、`stage1-light-768.png`、`stage1-light-1440.png`。

## 用户验收步骤

1. 打开 [开发工作台](http://127.0.0.1:8080/)，检查浅色主题、字号、左上角 Logo 和左侧导航；点击输入框确认没有黑边。
2. 点击任务灵感填入草稿，切换 Agent／知识库／任务记录，再新建对话确认输入清空；发送按钮目前禁用。
3. 在左侧切换主题、展开详情；按 Escape 确认抽屉关闭，焦点回到触发按钮。手机宽度下检查导航展开与收起。
4. 进入“设置”，确认 API 为 ready，postgres／redis／storage 均正常。打开 Docker [健康接口](http://127.0.0.1:18000/health/ready) 和 [开发 API 文档](http://127.0.0.1:18000/docs)；本机启动的 API 仍使用 8000。
5. 可在根目录执行 `powershell -ExecutionPolicy Bypass -File scripts/test-stage1.ps1 -IncludeContainers -IncludeUI` 复跑。需要 Docker Desktop、Node ≥22.12、本地 Python 3.12 虚拟环境和 Edge；故障演练会短暂中断当前开发服务并恢复。
6. 确认后回复“阶段 1 验收通过”，再进入阶段 2 身份、工作空间与权限。

## 当前限制

### Milvus 切换与 pgvector 清理

按用户要求，需求、架构、说明书、配置、迁移、健康检查与测试已同步改为 PostgreSQL 关系数据 + Milvus 向量数据。Milvus 2.5.14 使用独立 etcd 与现有 MinIO 的专用 bucket；端口 29530／29091。当前仅完成向量基础设施，RAG 与租户过滤仍按阶段 4 实现。

切换前数据库备份在 `.cache/postgres-before-milvus.sql`。已移除 pgvector 的 Python 声明、锁文件和两份依赖导出，卸载虚拟环境包，删除项目 uv 的包索引、wheel 元数据及对应解包归档。Docker pgvector 镜像已删除；其他项目镜像、全局 Docker 构建缓存与数据卷没有执行批量清理。历史迁移及历史报告中的扩展名称仅保留为兼容记录。

本轮结果：后端 40 通过、3 跳过，前端接口 2 通过；真实 Milvus 集合创建、写入、相似检索和清理通过，Milvus 停机时 ready=503／live=200，重启恢复为 ready=200。真实基础设施共九项通过，八个常驻服务健康。具体结果见 `artifacts/stage1-infrastructure.json`。

### 本机迁移修复验证

用户本机没有 `.env` 时，旧默认地址使用 5432／6379／9000，而开发 Compose 映射为 25432／26379／29000，导致迁移连接超时。已统一默认端口，并增加默认值、示例配置、Compose 映射一致性测试。

同时修复 Windows 普通启动的 Psycopg 事件循环兼容问题，以及 MinIO 内部健康探测被系统代理转发的问题。实际执行用户原命令 `python -m alembic upgrade head` 成功，`alembic current` 返回 `0001_vector (head)`；通过 `start.py` 启动独立端口的本机 API，`/health/ready` 返回 200，postgres／redis／storage 全部为 true。临时测试进程已关闭，未关闭用户运行的服务。

本轮后端测试 32 通过、3 跳过，Ruff 与 19 个自有模块的 mypy 通过；前端未修改，沿用已有通过结果。

### 本机与 Docker API 端口冲突修复

用户复跑容器验收时，Docker 报宿主机 8000 无法绑定。已将容器 API 宿主端口改为 `API_BIND_PORT`（默认 18000），本机开发保持 8000，容器内部保持 8000。Docker 前端仍使用 8080、Vite 仍使用 5173。无需停止其他进程即可运行两个环境。

测试脚本失败提示现在包含实际子命令和原始退出码，并在 Docker 失败时补充容器状态；应以提示之前的 Compose 原始错误定位原因。

完整复跑同时发现 Nginx 固定解析旧 API 容器地址，重建后出现 502。已使用 Docker DNS 动态解析，验收请求也不再经过系统代理。新增真实 API 容器重建后前端代理恢复验证。最终完整脚本退出码 0：后端 32 通过、3 跳过，前端接口 2 通过，浏览器 5 通过，七项真实基础设施检查全部通过。

- 阶段 1 只完成工程基础，登录、真实模型、聊天流式响应、工具执行、RAG、记忆、业务审批尚未实现。配置接受真实模型字段不代表已有模型适配器。
- 开发 Compose 使用明确的开发凭证与本机 HTTP，不是生产部署；生产上线要求阶段 0–7 通过，生产覆盖配置、安全扫描、HTTPS、备份恢复在阶段 6 处理。
- MinIO 健康检查验证进程就绪，不验证业务文件上传权限或 bucket；对象存储业务接入随知识库实现。
- CI 文件已提供，构成它的本地检查已执行；仓库尚未连接远端，不能宣称 GitHub Actions 已运行成功。
- Vite 构建有 Radix 的 `use client` 指令提示。当前应用是浏览器 SPA，构建与抽屉焦点验证通过；未来引入服务端渲染时重新验证。

![默认浅色桌面](./artifacts/stage1-light-1440.png)

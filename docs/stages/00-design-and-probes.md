# 阶段 0：设计与兼容性验证验收报告

日期：2026-10-09（Asia/Shanghai）。状态：**用户已明确回复“阶段 0 验收通过”，阶段 0 已通过验收。**

## 范围与交付

对应需求第 6、9、10、11、12、13、15 节：技术选择、架构、数据模型、API 契约、UI 草图、兼容性试验、测试基线、中文注释和项目说明书入口。

本阶段是设计与可运行验证程序，尚未实现登录、聊天、知识库或生产部署。`backend/start.py`、正式开发/生产 Compose 和 CI 在后续阶段实现。

- 后端 `pyproject.toml`、`uv.lock`、`requirements.probe.txt`：锁定依赖。
- 前端 `package.json`、`package-lock.json`：锁定依赖与工具链。
- `backend/probes/`、`backend/tests/`：工具契约、FastAPI 兼容、中断恢复、真实队列和重启验证。
- `deploy/archive/stages/docker-compose.stage0.yml`、`backend/Dockerfile.probe`：隔离试验环境，基础镜像摘要固定。
- `docs/ARCHITECTURE.md`、`API.md`、`TEST_PLAN.md`、`adr/0001-runtime-and-worker.md`、`PROJECT_MANUAL.md`。
- `docs/ui/workbench.html`：深浅主题、响应式导航与任务详情草图。
- `scripts/test-stage0.ps1`：失败即停止的验收入口，集成与浏览器测试使用明确开关。

## 实际环境

Windows 11 10.0.26200，12 个逻辑 CPU；物理内存未读取。未执行压测，不宣称达到上线性能目标。

| 组件 | 实测版本 |
| --- | --- |
| Python | 本地 3.12.14；Linux 容器 3.12.12 |
| Node / npm | 26.10.0 / 11.19.1 |
| Docker / Compose | 29.8.0 / 5.5.1 |
| FastAPI / Pydantic | 0.143.0 / 2.14.0 |
| LangGraph / PostgreSQL Saver | 1.2.14 / 3.1.2 |
| Celery / psycopg | 5.6.3 / 3.3.6 |
| React / TypeScript / Vite | 19.3.0 / 6.0.3 / 8.3.4 |
| Radix Dialog / Playwright | 1.2.0 / 1.64.0 |

完整传递依赖见锁文件。Celery 正式 Worker 只支持 Linux 容器；Windows 原生不是正式运行平台。

## 测试结果

| 检查 | 结果与证据 |
| --- | --- |
| 本地后端 | 11 通过，3 个集成用例明确排除；见 `artifacts/backend-tests.xml` |
| Linux 容器全量 | 14 通过，5.70 秒；包含本地测试、真实 Redis 投递、PostgreSQL 恢复、pgvector 距离与 Worker ping |
| Worker 重启 | 同一 thread_id 重启前 `waiting=true`，重启后 `waiting=false,status=approved` |
| Ruff lint / format | 通过，8 个 Python 文件格式符合规范 |
| mypy strict | 通过，5 个自有模块无类型错误；仅豁免 Celery 第三方缺失的类型标记 |
| 前端工具链 | React、TypeScript、Vite、Radix 导入检查通过 |
| Playwright UI | 最新应用布局 7 项通过，覆盖独立视图、左上角 Logo、输入与抽屉交互；见 `artifacts/ui-test-results.json` |
| 可访问性 | 360/768/1440 px、深浅主题的 axe WCAG 2 A/AA 检查无违规；不等于完整人工合规认证 |
| Compose 配置 | 配置校验通过 |
| 验收脚本 | `powershell -File scripts/test-stage0.ps1` 通过，默认明确跳过集成与 UI，二者已单独执行 |
| npm 安装审计 | 安装时 164 个包，0 已报告漏洞；不替代发布安全扫描 |

本地 11 项与容器 14 项存在包含关系，不可相加为 25 项独立测试。

## 已修复问题

- 默认临时目录受权限限制：改用项目内 `--basetemp` 并在允许的执行环境重跑。
- Worker 配置推断为普通字典：使用 `RunnableConfig` 明确契约，严格类型检查通过。
- 浅色主题切换时背景渐变与新文字色混合造成短暂对比度不足：改用透明度过渡，UI 全部复测通过。
- Celery 不支持 Windows：真实 Worker 验证采用 Linux 容器。
- Windows PowerShell 5 错误识别无 BOM 的中文脚本：验收脚本保存为 UTF-8 BOM，入口重跑通过。

## 复现命令

从根目录执行基础检查：

```powershell
powershell -File scripts/test-stage0.ps1
```

真实容器测试：

```powershell
docker compose --project-directory . -f deploy/archive/stages/docker-compose.stage0.yml up -d --build --wait worker
docker compose --project-directory . -f deploy/archive/stages/docker-compose.stage0.yml --profile test run --rm --no-deps tests
```

UI 测试先在独立终端启动 `python -m http.server 4170 --bind 127.0.0.1`，再执行：

```powershell
cd frontend
npx playwright test --config playwright.stage0.config.ts
```

Worker 重启验证使用全新 thread_id：

```powershell
docker compose --project-directory . -f deploy/archive/stages/docker-compose.stage0.yml exec -T worker python -m probes.restart begin --thread stage0-restart-20261009-review
docker compose --project-directory . -f deploy/archive/stages/docker-compose.stage0.yml restart worker
docker compose --project-directory . -f deploy/archive/stages/docker-compose.stage0.yml exec -T worker python -m probes.restart resume --thread stage0-restart-20261009-review
```

停止环境且保留数据：`docker compose --project-directory . -f deploy/archive/stages/docker-compose.stage0.yml down`。不要添加 `-v`，它会删除本项目验证卷。

## 用户验收步骤

1. 打开 `http://127.0.0.1:4170/docs/ui/workbench.html?v=3` 或直接打开 HTML，确认参考图配色与对话应用布局。
2. 检查左上角 Logo 与左侧全部选项，切换 Agent、知识库、任务记录、设置与对话视图；切换主题、打开详情，手机使用左侧导航。
3. 查看 `artifacts/` 中深浅主题的 360/768/1440 px 截图。
4. 阅读架构、API 与 ADR，确认运行环境、权限设计和技术取舍。
5. 可复跑上述测试，确认后回复“阶段 0 验收通过”，再进入阶段 1。

## 限制与未执行项

- 无真实模型凭证，仅验证 Mock 工具契约，不证明真实供应商兼容；阶段 3 必须真实联调。
- 未执行生产部署、压测、备份恢复、业务鉴权、幂等或发布安全扫描，它们尚未实现。
- 重启验证证明检查点恢复，不证明外部副作用恰好执行一次；阶段 5 单独实现与测试。
- UI 为静态草图，发送禁用，没有真实会话；v2 已实现窄屏抽屉交互，正式 React 组件在前端业务阶段实现。
- 本地与容器 Python 补丁版本分别通过验证；生产运行时在阶段 6 统一并复测。

## 下一阶段

### UI 第二版修改记录

用户拒绝第一版视觉，指定参考 Awwwards Design Agencies。v2 重新设计顶部导航、主视觉、几何主体和对话版面，采用深墨色/暖白与黄绿色强调；参考来源和设计规范见 [UI 设计说明](../UI_DESIGN.md)。此处为历史修改记录；最新验收状态见本文开头与阶段 1 报告。

### 最新 UI 修改记录

用户提供橙色外背景、黑色圆角面板和彩色图标参考图，并进一步要求类似 DeepSeek 的独立页面布局、Logo 左上角、所有选项左置。最新草图已取消长页介绍，使用固定视口、左侧导航、中心输入区和独立内容视图。7 项 UI 测试通过，新增视图切换、新建对话、灵感填入和 Logo 位置验证；真实业务仍未实现。最新图片为 `artifacts/ui-dark-1440.png` 等，旧版封面不代表当前设计。

追加用户要求：默认浅色主题，欢迎标题桌面 28px、手机 24px，输入与正文 16px、导航 14px；两种主题及三种视口重新执行 UI 验收。当前默认截图为 `artifacts/ui-light-1440.png`。

验证命令：`cd frontend` 后执行 `npx playwright test --config playwright.stage0.config.ts`。后端无改动，沿用此前 14 项容器测试与重启恢复结果；本轮只重跑受到修改影响的 UI 验收。

用户已明确确认阶段 0。阶段 1 已提供 React/FastAPI 骨架、中文注释、统一启动文件、配置校验、健康接口、开发 Compose、CI 与冷启动验收，见 [阶段 1 报告](./01-scaffold.md)。

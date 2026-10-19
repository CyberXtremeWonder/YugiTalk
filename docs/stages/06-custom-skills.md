# 自定义 Skill 补充验收

日期：2026-10-11。作为阶段 6 开发期间的功能增补交付，人工验收待用户确认；不代表整体阶段 6 或正式上线通过。

## 交付结果

- 左侧 Skill 管理页：创建、编辑发布、启停、删除；空间共享与成员管理权限。
- 对话输入框底部选择一个 Skill，执行流程只展示名称、阶段与工具。
- 完整版本与 SHA-256 固定，运行时复核撤销；工具权限取交集；RAG 无证据仍固定拒答。
- 独立前后端模块、中文类与函数说明；需求、README、说明书、目录说明与 Vibe Coding 复盘同步。

SuperMew 参考来源、操作步骤、接口和限制见 [自定义 Skill](../CUSTOM_SKILLS.md)。尚未开放文件包导入、自动技能路由或自定义脚本。

## 实际验证

| 检查 | 结果与证据 |
| --- | --- |
| Skill 专项 | 19 通过，`artifacts/custom-skills-tests.xml` |
| 完整后端回归 | 227 通过、3 个环境集成测试跳过，`artifacts/custom-skills-full-tests.xml`；跳过不算通过 |
| 前端基础回归 | 24 通过，`artifacts/custom-skills-frontend-tests.xml` |
| 真实页面和 API | 创建、v2 发布、绑定、只读工具统计、流程、启停、删除后隐藏、390px 无溢出通过，`artifacts/custom-skills-ui-validation.json` |
| 页面验证环境 | 独立内存 SQLite + Mock，不修改用户业务数据，不调用付费模型 |
| 类型和代码检查 | Ruff、格式检查、严格 MyPy、前端 ESLint、TypeScript 构建和 Prettier 通过 |
| 中文说明 | Python 667 个声明、前端 64 个声明，无缺失 |
| 开发部署 | 页面、存活、就绪均 HTTP 200；10 个运行服务健康；新接口和任务输入契约存在，`artifacts/custom-skills-development-validation.json` |
| PostgreSQL | 已迁移 `0009_custom_skills`，新增表的外键与唯一索引建立；现有文档状态仍为 3 个可用、3 个删除 |
| 数据保护 | 迁移前 PostgreSQL 归档保存在项目 `data/backups/`，已核对目录可读，未宣称完整恢复演练 |
| 缓存与 Docker | 项目 C 盘 UV 依赖缓存检查释放 0 MiB；无待删的项目闲置镜像，保留两份仍被容器引用的镜像 |

真实 PostgreSQL 目录和索引检查不能替代并发发布压力测试；离线模型执行不能替代真实模型对技能格式、工具选择和知识库遵循质量的评测。独立生产验收栈本轮未更新，不沿用旧报告证明新技能已正式上线。

## 人工验收

开发入口：http://127.0.0.1:8080/#skills 。

1. 创建 `concise-report`，填写中文流程并选择文本统计；发布成功后目录出现 v1。
2. 修改流程，发布后变为 v2。对话页能选择该版本。
3. 停用后回到新建对话，新技能选择目录不再提供该项；启用后可再次选用。
4. 删除后目录隐藏，已有任务历史仍保留技能身份。
5. 无费用完整链路可运行 `node scripts/check-custom-skills-ui.cjs`，验证真实页面任务、模型协议和工具执行。

现有开发模型可能为 DeepSeek，手动发送会请求配置的供应商。正式模型质量、真实服务器、受信任 HTTPS、外部 S3、异机备份和运维责任安排继续按 [阶段 6 生产报告](06-production.md) 验收。

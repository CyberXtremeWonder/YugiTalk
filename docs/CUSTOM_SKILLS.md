# 用户自定义 Skill

更新：2026-10-11。功能已接入本地开发环境；阶段 6 正式上线门槛继续独立验收。

## 使用与示例

1. 登录工作台，在左侧进入 **Skill**，填写名称、描述与 Markdown 流程。
2. 名称使用小写英文、数字和连字符，例如 `concise-report`；描述与正文支持中文。
3. 按需勾选文本统计、报告排版，点击“创建 Skill”。后续编辑点击“发布新版本”。
4. 回到“对话空间”，在输入框底部选择技能后发送任务；流程显示技能名称及实际工具调用。
5. 停用后不可新用，删除后目录立即隐藏。旧任务历史仍保留原版本。

可复制的技能正文：

```markdown
# 文本分析流程
1. 确认用户提供了待分析文本；缺少输入时询问补充。
2. 使用已授权的文本统计工具，不虚构统计结果。
3. 用三个要点说明结果，明确缺失信息。
4. 选用知识库时只依据资料回答；没有证据则回复“没有相关知识”。
```

正文是模型的流程说明，真实模型遵循质量需要另行评测。Mock 验收证明加载、权限和执行链路，不证明真实模型一定按格式回答。

## SuperMew 参考与首版范围

参考本地 `E:/Code/SuperMew-main` 的 `docs/adr/0017-skill-tool-registry.md`、`backend/capabilities/control_contracts.py`、`control_service.py`、`backend/skills/registry.py`、`backend/agent/context.py`。

沿用“元数据 + 流程正文 + 允许工具”、目录摘要、按需加载、每任务一个技能、版本和 SHA-256 固定、运行时权限检查。完整正文只在用户选择技能后注入，不把整个技能目录加入模型上下文。

首版通过页面编辑与数据库发布，尚未实现自动技能路由、`describe_skill` 控制工具、`skill.yaml` 文件包导入、工具市场或任意脚本执行。后续扩展仍须遵循注册工具、版本与权限边界。

## 权限、版本和数据

| 行为 | 规则 |
| --- | --- |
| 查看、使用、创建 | 当前空间成员；创建后在空间内共享，其他空间不可访问 |
| 编辑、启停、删除 | 创建者，或当前空间管理员、所有者 |
| 发布 | 写入新版本，不覆盖原版本或已有任务 |
| 任务绑定 | 保存完整版本快照与校验值；幂等请求包含技能版本 |
| 工具执行 | Agent 授权与 Skill 允许工具的交集；不勾选表示禁止全部业务工具 |
| 执行、重试、澄清恢复 | 复核成员、技能可用性及原版本哈希；编辑新版本仍保留原任务说明 |
| 停用、删除 | 未完成任务在后续执行边界中止；删除采用软删除保留审计与历史 |
| 知识库无证据 | 固定回复“没有相关知识”，不调用通用回答模型；技能说明不能覆盖该服务端规则 |

名称最多 64 字符，描述 500 字符，正文 6000 字符；每空间最多 100 个未删除技能，每技能最多 100 个版本。模型整体上下文超过现有 32768 字节上限时明确失败，不静默截断流程。正在外部服务内执行的请求不能凭停用操作瞬间撤回，但返回结果和后续调用会继续检查资格。

PostgreSQL 的 `skills`、`skill_versions` 保存定义；`runs.snapshot` 保存任务固定内容。本地持久目录继续使用项目 `data/postgres/`，生产按独立 Compose 项目数据目录配置。无需新增 Docker 卷、下载依赖或迁移 BGE-M3 缓存。

## 接口和代码入口

| 接口 | 用途 |
| --- | --- |
| `GET /api/v1/workspaces/{id}/skills` | 分页摘要，不返回正文 |
| `POST /api/v1/workspaces/{id}/skills` | 创建并发布 v1 |
| `GET /api/v1/skills/{id}` | 最新完整说明 |
| `POST /api/v1/skills/{id}/versions` | 发布新版本 |
| `PATCH /api/v1/skills/{id}` | `{ "enabled": false }` 启停 |
| `DELETE /api/v1/skills/{id}` | 软删除 |
| `POST /api/v1/runs` | 可选 `skill_version_id`，未选择为 `null` |

后端 `backend/app/skills/`，前端 `frontend/src/skills/`；任务接入 `tasks/router.py`、`tasks/execution.py`，提示词接入 `memory/context.py`，人工澄清恢复接入 `rag/resume.py`。迁移 `0009_custom_skills` 只新增表和索引。

写接口沿用 Cookie、来源白名单、CSRF 和审计。不要在技能正文填写密钥。自定义指令不是额外工具权限，也不能注册 Shell 或未授权 HTTP 请求。

技能正文已改为用户级数据消息，不再作为系统指令；只有代码固定安全策略进入系统层。旧检查点恢复同样重新处理消息层级。回复图片不加载，网址显示为不可点击文字；实现原理与局限见 [Prompt 注入防护](./PROMPT_INJECTION_DEFENSE.md)。

## 自动验证和人工验收

项目根目录运行：

```powershell
backend/.venv/Scripts/python.exe -m pytest -c backend/pyproject.toml backend/tests/test_skills.py -q -p no:cacheprovider --basetemp=.cache/pytest-skill-manual
npm --prefix frontend run build
node scripts/check-custom-skills-ui.cjs
```

浏览器验收需要 Edge、项目虚拟环境和前端依赖，临时服务占用 `127.0.0.1:18009`。使用真实 FastAPI、独立内存 SQLite、离线 Mock 和真实页面；退出后不保留业务数据，不访问用户 PostgreSQL、知识库或付费模型，不读取模型密钥。它不能代替 PostgreSQL 并发、真实模型质量或正式服务器验收。

Skill 专项 19 项通过，包括跨空间、CSRF、非法工具、版本冻结、幂等、工具交集、模型越权、执行中撤销、重试、RAG 澄清恢复、空库拒答和完整上下文上限。完整后端回归 227 项通过、3 项环境集成测试跳过；前端基础回归 24 项通过，另有真实页面完整流程及 390px 窄屏验证。完整记录见 [补充验收报告](./stages/06-custom-skills.md)。报告在 `docs/stages/artifacts/custom-skills-*.xml`、`custom-skills-ui-validation.json`，截图为 `custom-skills-desktop.png`、`custom-skills-chat.png`、`custom-skills-mobile.png`。

人工验收：创建示例技能 → 发布新版本 → 对话选择 → 检查简短流程 → 停用 → 删除并确认目录隐藏。开发 `.env` 若配置真实模型，发送任务会使用供应商并产生费用；无费用链路验证使用上述隔离脚本。

## 更新与回退

更新前备份为项目 `data/backups/skills-before-0009-20261011T062938Z.dump`，已验证 PostgreSQL 归档目录可读取；这是迁移保护，不是完整恢复演练。迁移后原资料仍为 3 个可用、3 个已删除。

其他开发环境先独立备份，再更新：

```powershell
docker compose build api frontend
docker compose run --rm --no-deps migrate
docker compose up -d --no-deps --force-recreate --wait api worker scheduler frontend
```

迁移失败则停止后续更新。旧代码可保留新增表，禁止直接 downgrade：降级会删除全部技能和版本。正式服务器使用独立生产发布流程。本轮仅更新开发栈，没有把原阶段 6 验收报告当作新功能的正式上线证明。

# 第一版知识图谱与 RAG 融合

本版保留现有 Dense + BM25、父块合并、重排、评分和原文引用。在查询中额外识别实体，最多扩展两跳，将关系所指向的原文分块与已有候选按 RRF 融合。图谱文本不直接成为答案证据；最终来源仍读取 SQL 原文并复核权限和版本。

## 使用方式

1. 更新数据库至 `0017_knowledge_graph`，更新 API、Worker、Scheduler 和前端。必须先迁移再启动新版本服务。
2. `RAG_GRAPH_ENABLED=true` 默认启用。新文件普通索引完成后，后台自动登记图谱构建任务，Scheduler 负责投递。
3. 知识库文件下展开“知识图谱与原文依据”，可查看构建状态、实体数量、关系和连续原文片段。
4. 已有文件不会自动批量补建。点击“构建图谱”或“重新构建图谱”，不重新调用 Embedding、不增加文档索引版本。
5. 按原来的方式选择知识库提问。图谱有结果时参与召回；未构建、构建失败或关闭功能时继续使用普通 RAG。

真实抽取复用 `RAG_FAST_MODEL`，未配置时使用默认聊天模型及当前供应商凭证。会增加模型调用和费用。Mock 模式只验证任务流程，明确显示演示模式，不伪造实体和关系。切换真实模型后需重新构建演示图谱。

按现有部署流程执行迁移，例如在已配置数据库的后端目录执行：

```powershell
.venv/Scripts/python.exe -m alembic upgrade head
```

Docker 部署继续使用现有 migrate 服务，并更新 api、worker、scheduler、frontend。该说明不表示本轮已迁移业务库或重启服务。

## 数据与可靠性

- `graph_builds`：文档代次、独立构建状态、模式、失败原因、执行令牌、租约和投递时间。
- `graph_entities`：文档内名称、类型和明确别名。不同文档实体保持独立；同名同类型仅用于扩展候选，不宣布身份相同。
- `graph_facts`：一条关系的一份原文依据，包含两端实体、分块、引文和文档版本。多文档可分别支持同一关系；删除一个文档不影响其他文档的依据。

抽取结果必须通过结构校验；名称、别名必须出现于原文，关系引文必须是连续原文且包含双方名称或别名。该校验保证来源可定位，不能证明模型对关系语义的理解一定正确，仍需质量评测。

原文索引发布与图谱排队意图在同一事务提交。图谱抽取结束后在文档锁下再次校验状态、版本和令牌，并一次性发布。半成品不参与检索。每次模型调用及读取期间检查文档与账号状态。删除、重新索引、旧令牌失效均阻止旧构建发布。

调度器每次最多扫描 100 个待处理任务，30 秒后可补投；执行窗口 180 秒、租约 200 秒。失联任务最多领取 3 次，模型或校验失败需手动重试。重复队列消息不重复构建已领取任务。

图谱查询异常在 SQL 保存点内回退，流程记录 `graph_candidates=-1`；后续原文授权校验不会被吞掉。关闭功能不会删除已有图谱。

## 首版边界

- 每份文档最多 64 个分块；超限明确失败，不发布前 64 块的残缺图谱。普通索引仍可用。
- 每块最多 12 个实体、12 条关系，模型输出最多 4096 Token。模型超时、结构输出错误不会伪装成抽取成功。
- 实体类型：人员、组织、系统、产品、概念。关系：负责、属于、依赖、使用、关联。
- 查询实体名称与明确别名采用文本匹配，未加入实体向量检索、模糊消歧、社区摘要或任意深度路径搜索。
- 召回最多读取 10000 个有效候选实体；超过则跳过图谱召回。最多 8 个种子、每跳最多 32 个节点、每节点最多 8 条关系，最多两跳，受现有总候选数限制。
- 原有最终 5 条来源和上下文长度限制保持不变。复杂路径可能因候选截断或重排缺失部分依据；不能保证完整多跳推理，仍由原文评分决定能否回答。
- 页面最多展示 100 条关系依据，明确提示还有更多。

## 接口与代码

- `GET /api/v1/documents/{id}/graph`：按文档权限读取状态和原文关系。
- `POST /api/v1/documents/{id}/graph`：需要登录、写权限和 CSRF 校验，登记补建或重建任务。
- `backend/app/rag/graph.py`：抽取、发布、恢复、实体匹配、两跳召回和融合。
- `backend/app/rag/graph_models.py`：图谱数据实体。
- `backend/app/rag/pipeline.py`、`indexing.py`：聊天及直接检索接入。
- `frontend/src/rag/GraphPanel.tsx`：状态与原文关系查看入口。

## 验证与验收

自动回归覆盖原文伪造、无效端点、别名校验、跨文档两跳、图谱独立失败、重试幂等、权限与 CSRF、过期版本、删除中断、宕机恢复上限、图谱独立召回、关闭开关、查询故障回退、文件上限和迁移升降级。

2026-10-16 验证结果：

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| 后端全量回归 | 366 通过、3 跳过；跳过项需要显式启动历史阶段零队列 | [测试报告](./stages/artifacts/knowledge-graph-tests.xml) |
| 图谱专项 | 16 通过，包含迁移升降级、重新索引隔离及前述边界 | `backend/tests/test_knowledge_graph.py` |
| 真实 PostgreSQL 16 | 全迁移、真实外键发布、幂等、两跳召回、版本与删除隔离、降级升级保留文档通过 | [数据库报告](./stages/artifacts/knowledge-graph-postgres.json) |
| 前端 | 类型检查、生产构建、47 项现有测试、ESLint 通过 | 本轮本机验证 |
| 浏览器组件交互 | 展开、补建及 CSRF、状态刷新、原文关系、重建入口通过 | [页面报告](./stages/artifacts/knowledge-graph-ui.json)、[截图](./stages/artifacts/knowledge-graph-ui.png) |
| 后端静态检查 | 本轮图谱及接入文件 Ruff、7 个变更模块的严格类型检查通过 | 本轮本机验证 |

全量回归第一次运行有 30 项测试临时目录权限错误，使用允许访问项目专用测试目录的环境重新运行后通过。全局中文说明检查有 7 处既有测试函数缺少说明，均不属于本轮新增文件，本轮未改写这些测试。

真实数据库验证只连接固定本机端口 55437 的全新临时库，使用内存存储，验证后已删除容器；没有迁移业务库。浏览器使用真实前端组件与接口替身，测试使用模型与存储替身，真实付费模型调用为 0。完整业务页面联调及真实模型抽取质量仍需在更新服务后验收。

可复现图谱专项与页面验证：

```powershell
backend/.venv/Scripts/python.exe -m pytest -c backend/pyproject.toml backend/tests/test_knowledge_graph.py -q -p no:cacheprovider --basetemp .cache/pytest-graph-verify
node scripts/check-knowledge-graph-ui.mjs
```

真实数据库验证使用本机已有的 `postgres:16-bookworm` 镜像；只创建独立临时容器，脚本会拒绝非空数据库：

```powershell
docker run --detach --rm --name yugitalk-graph-verify --pull=never --publish 127.0.0.1:55437:5432 --env POSTGRES_USER=graph_verify --env POSTGRES_PASSWORD=graph-verify-local-only --env POSTGRES_DB=graph_verify --tmpfs /var/lib/postgresql/data postgres:16-bookworm
# 等待数据库接受连接后执行。
backend/.venv/Scripts/python.exe scripts/check-knowledge-graph-postgres.py
docker stop yugitalk-graph-verify
```

推荐使用两份资料：“支付服务使用 DB1。”和“订单服务依赖支付服务。”，构建图谱后核对引文，再询问“DB1 故障可能影响哪些服务？”。确认回答引用两份原文；删除第二份后确认不再将其作为依据。另准备同名歧义、否定关系和无答案问题，对比关闭图谱时的回答正确性、引用完整性、耗时与模型成本。

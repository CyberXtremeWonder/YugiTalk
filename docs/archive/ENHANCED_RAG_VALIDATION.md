# 七步 RAG 历史升级与验收记录

保留原先记录的版本、测试数量、操作和入口。这里的“当前”“本轮”指记录发生时，不代表 2026 年 11 月的运行状态。当前行为见 [增强 RAG](../ENHANCED_RAG.md)。

## 数据迁移与已有资料

新增关系库迁移 `0008_rag_hierarchy`：父分块表与子分块的父级引用，不改写已有资料。隔离阶段六数据库已升级到此版本；开发数据库本轮没有升级，也没有替换开发容器。

新上传资料自动写入带原生 BM25 的版本化混合集合。旧稠密集合继续可查，流程记录 `legacy_dense_documents`，不会把它伪装成原生 BM25。分析器、字段和父级策略参与集合版本；发现不匹配结构会报错，禁止静默降级。

旧资料可显式升级：在能够连接开发依赖的项目环境执行下列命令，将示例 UUID 替换为指定资料 ID。默认只查看；`--apply` 才写入新集合。服务器使用其私有环境变量及对应网络连接，不能照搬本机地址。

```powershell
backend/.venv/Scripts/python.exe scripts/rebuild-rag-index.py --document "资料UUID"
backend/.venv/Scripts/python.exe scripts/rebuild-rag-index.py --document "资料UUID" --apply
```

重建复用已保存的同版本向量，不重新下载或生成 Embedding；校验候选完整性后原子切换资料集合指针。失败时保留已发布旧索引。单次限 1000 个分块，不自动批量迁移全部资料，旧集合暂保留以便核对。索引迁移前应备份业务数据；本轮隔离数据库迁移前的备份位于 `data/stage6-production/backups/enhanced-rag-*.dump`。

## 自动验证结果

| 验证 | 本轮结果 | 证据 |
| --- | --- | --- |
| 后端全量回归 | 184 通过、3 跳过；跳过项为需独立阶段零 Worker 的历史测试 | [测试报告](../stages/artifacts/enhanced-rag-tests.xml) |
| 七步针对性测试 | 23 项通过，覆盖并行取消、截止时间、合并、版本权限、重写上限、熔断、澄清幂等及重建失败回退 | `backend/tests/test_enhanced_rag.py` |
| 真实存储集成 | BGE-M3 1024 维、Milvus 2.5.27；原生 BM25 命中、混合并行、父级合并、旧索引升级、删除隔离通过 | [集成报告](../stages/artifacts/enhanced-rag-integration.json) |
| 浏览器 | 同一 Run 暂停、重载后打开历史、补充恢复、答案引用、原文核验、删除测试资料 | [浏览器报告](../stages/artifacts/enhanced-rag-ui.json) |
| 付费调用 | 0 次 | 集成使用离线规则，浏览器使用独立模型协议替身；不是正式模型质量评测 |

前端单元测试、构建、类型检查、代码风格和中文说明检查也纳入本轮验证。Milvus 2.5.14 的开发栈尚未在本轮重启验证，不扩大本轮 2.5.27 的集成结论。

可复现命令（从项目根目录执行）：

```powershell
$env:PYTHONUTF8='1'
backend/.venv/Scripts/python.exe -m pytest -c backend/pyproject.toml backend/tests/test_enhanced_rag.py -q -p no:cacheprovider --basetemp .cache/pytest-enhanced-rag
# 需隔离阶段六服务已运行；脚本初始化专用测试账号，完成后取消任务并删除测试资料。
backend/.venv/Scripts/python.exe scripts/run-enhanced-rag-ui.py
```

真实存储探针仅允许隔离阶段六配置，禁止在业务环境批量写测试资料：

```powershell
Get-Content -Raw -Encoding UTF8 scripts/check-enhanced-rag.py | docker compose --env-file .cache/stage6-production.env -f docker-compose.yml -f docker-compose.prod.yml -f deploy/tests/docker-compose.stage6-test.yml -p yugitalk-stage6 exec -T api python -u -
```

## 你现在可以这样验收

2026-10-11 开发修复追加验收：当前 **http://127.0.0.1:8080** 已更新为“无证据固定拒答”版本。选择空知识库，问“你知道薛定谔的猫吗”，应只看到“没有相关知识”，任务正常完成且无来源；刷新后从历史打开仍显示相同结果。无关资料与纠错后仍不足由后端回归验证。开发浏览器验收使用独立身份与空库，没有上传业务资料，没有追加真实模型调用；报告见 [空库页面验收](../stages/artifacts/rag-no-knowledge-ui.json)。

评分故障修复全量结果为后端 208 通过、3 跳过，前端 24 通过；针对性回归 40 项通过。经用户明确授权，曾对原失败任务做一次有界 DeepSeek 评分诊断，返回 `no_knowledge`，原任务未修改；历史异常缺少类型日志，不能据此确定原失败原因。本轮随后均使用离线传输替身或空库测试。正式隔离入口 18443 仍运行上一组镜像，本次只更新开发栈，以下七步历史验收记录不扩展为本轮生产复验。

1. 打开当前独立验收入口 **https://localhost:18443**。本机测试证书、测试账号与私有密码位置见 [部署说明](../DEPLOYMENT.md)；此入口使用协议替身，开发入口 8080 本轮未启动。
2. 创建知识库，上传包含“Milvus 保存 BGE-M3 向量，PostgreSQL 保存资料元数据”的 TXT 或 Markdown。等待导入完成，确认文件出现在右侧且流程重置。
3. 在对话框选择这个知识库，问“Milvus 保存什么？”；确认流程简短显示检索、证据核对和答案，正文不出现 `[S1]`、`[S2]`；展开“检索来源”，点击文件名查看原文及 `E-…` 来源身份。生成中与重新打开历史消息时均隐藏索引。
4. 问“比较 Milvus 和 PostgreSQL 的职责，以及它们如何配合。”；确认流程进入复杂问题处理。协议替身答案内容固定，算法分支和计数以自动测试轨迹为准。
5. 问“它的 Milvus 用途是什么？”；独立替身会触发澄清。刷新页面后从左侧打开该会话，补充“指的是 Milvus，它保存 BGE-M3 向量。”；确认继续同一任务，任务记录不多出一个新 Run。
6. 删除测试资料，再检索原内容；确认删除文件不再显示、旧来源不能继续被当作当前证据。需要验证次数、熔断、越权和恢复等边界时运行自动测试。

通过以上步骤后可确认“七步增强 RAG 本轮人工验收通过”。这项确认独立于阶段 6 的正式上线验收；后续仍需在真实模型上运行版本化质量样例，比较命中、引用准确性、耗时与成本。

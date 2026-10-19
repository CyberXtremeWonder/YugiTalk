# 当前知识库检索评测

入口：`scripts/evaluate-knowledge-retrieval.py`。只调用现有登录、知识库列表及搜索接口，不上传到 LangSmith，不创建对话或生成答案，不修改知识库资料。搜索仍会调用你配置的 Embedding 服务。

## 直接运行

先按开发说明启动项目，在项目根目录执行：

```powershell
$env:PYTHONUTF8='1'
backend/.venv/Scripts/python.exe scripts/evaluate-knowledge-retrieval.py
```

默认读取已有 `backend/.env.rag-test` 的以下配置，不改写私有文件：

```dotenv
RAG_TEST_API_URL=http://127.0.0.1:18000
RAG_TEST_ORIGIN=http://127.0.0.1:18000
RAG_TEST_ACCOUNT=你的登录账号
RAG_TEST_KNOWLEDGE_BASE_ID=你的知识库ID
```

密码优先使用该文件内已有 `RAG_TEST_PASSWORD`，没有则隐藏输入。Origin 必须是后端允许的来源。脚本不需要 LangSmith 密钥或 Agent 版本 ID。登录只进行一次，单并发检索；鉴权、权限、目标不存在、限流或连接异常时停止后续请求，逐题记录未执行原因。

默认使用已有的 `outputs/agent_memory_qa/agent_memory_50_qa.csv`。该文件只有问题和参考答案，**没有标准来源标注，因此不能计算真实 Recall**。此模式会给出请求完成率、P50/P95 耗时、来源原文预览及“参考答案逐字覆盖率”代理指标。报告中的 Recall 显示“无有效标注／不适用”，不会用代理指标冒充召回率。这 50 题也只适合包含 Agent Memory 相关资料的知识库。

先试运行前 5 题：

```powershell
backend/.venv/Scripts/python.exe scripts/evaluate-knowledge-retrieval.py --limit 5
```

不知道知识库 ID 时：

```powershell
backend/.venv/Scripts/python.exe scripts/evaluate-knowledge-retrieval.py --list-bases
```

所有结果写入 `outputs/rag-evaluation/retrieval-时间戳.md` 和同名 `.json`。Markdown 是中文摘要；JSON 有逐题预期证据、实际来源、原文预览、命中情况和耗时。请求失败返回非零退出码，报告仍保留；登录失败或标注无效则直接报错。

## 测真实召回率

从已有 CSV 导出可编辑模板，此命令不连接服务：

```powershell
backend/.venv/Scripts/python.exe scripts/evaluate-knowledge-retrieval.py --export-template outputs/rag-evaluation/labeled-cases.json
```

打开模板，根据当前知识库**原文**，为每题增加 `relevant`。每个元素代表一份应当找回的证据；跨文档多跳题需要列出所有必要证据。示例中的文件名和引文必须换成你自己的真实资料：

```json
{
  "cases": [
    {
      "question": "DB1 故障可能影响哪些服务？",
      "relevant": [
        {"filename": "支付服务说明.md", "quote": "支付服务使用 DB1。"},
        {"filename": "订单服务说明.md", "quote": "订单服务依赖支付服务。"}
      ]
    },
    {
      "question": "知识库没有涉及的某个具体事项是什么？",
      "expected_no_results": true
    }
  ]
}
```

支持 `document_id`、`filename`、`chunk_id` 定位来源，建议优先使用 `document_id`，避免同名文件歧义。`quote` 可选，填写后要求引文逐字出现在一条返回证据中，忽略空白和英文大小写；仅按文件名标注测的是文件级召回。父块合并时也能通过 `child_ids` 匹配原始分块 ID。重新索引后分块 ID 可能变化，标注需相应更新。

未标注题不填写 `relevant`；`relevant: []` 会报错，避免把未标注题当成负例。不要只根据本次搜索结果挑选标准证据，否则容易高估召回。

运行标注集：

```powershell
backend/.venv/Scripts/python.exe scripts/evaluate-knowledge-retrieval.py --cases outputs/rag-evaluation/labeled-cases.json --k 5
```

切换知识库或配置文件：

```powershell
backend/.venv/Scripts/python.exe scripts/evaluate-knowledge-retrieval.py --cases outputs/rag-evaluation/labeled-cases.json --base 实际知识库ID --env-file backend/.env.rag-test
```

## 指标口径

| 指标 | 含义 |
| --- | --- |
| Recall@K | 每题找回的标注证据数 ÷ 应找回证据数，再对正例题取平均 |
| Hit@K | 前 K 条至少找到一份标注证据的正例比例 |
| 完整证据命中率 | 前 K 条找到全部标注证据的正例比例，多跳问答重点观察 |
| Precision@K | 每题相关返回条数 ÷ K；不足 K 条的位置按不相关处理 |
| MRR@K | 首条相关结果排名倒数的正例平均值；没有命中计 0 |
| 负例空结果率 | 标注应无结果的题目中，搜索确实返回空结果的比例 |
| P50/P95 | 成功搜索请求端到端耗时，含 Embedding；不含登录 |

正例指标仅统计成功请求且有标注的题，必须同时查看完成率及错误数。两份文档各支持一条必要关系，只找回一份时，Recall=50%、Hit=100%、完整证据命中=0%。

这个脚本评测 `/search` 最终证据，包含当前混合召回、图谱融合、父块合并和上下文截断；默认最多返回 5 条，因此 K 只支持 1–5。它不经过聊天中的模型重排、Grader 和重写，也不评价最终答案质量。当前服务是否使用图谱取决于实际部署版本、配置和已构建图谱。

比较图谱效果时，用同一知识库快照和标注集，在服务端关闭、开启 `RAG_GRAPH_ENABLED` 后各运行一次，保存两个报告。不要只修改测试脚本所在终端的变量，就认为已切换运行中的服务。

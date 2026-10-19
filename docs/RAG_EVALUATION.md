# 调用完整 RAG 与 LangSmith 评测

本项目通过知识库任务执行检索、评分、纠偏和最终回答。`RagPipeline.run` 只产生证据与路由状态，最终答案由任务执行器继续调用 Agent 图生成。因此完整评测使用 API，而非把管线返回值当成答案。

## 1. 准备

按启动手册启动 API、Worker 和知识库依赖；建立专用测试账号与空间，在知识库上传资料并等到 ready，在 Agent 页创建并发布助手。不要使用真实用户的私有资料做外部评测。

在 `backend/.env.rag-test` 创建私有配置（已被 .gitignore 排除）：

```dotenv
RAG_TEST_API_URL=http://127.0.0.1:18000
# 公网网关覆盖环境使用此来源；纯开发环境改为 http://127.0.0.1:18000。
RAG_TEST_ORIGIN=https://yugiagent.fun
RAG_TEST_ACCOUNT=你的测试账号邮箱
RAG_TEST_PASSWORD=你的测试账号密码
RAG_TEST_AGENT_VERSION_ID=已发布助手版本UUID
RAG_TEST_KNOWLEDGE_BASE_ID=知识库UUID
LANGSMITH_API_KEY=你的LangSmith密钥
RAG_TEST_DATASET=RAG
RAG_TEST_ALLOW_EXTERNAL=true
```

通过浏览器开发者工具Network查看知识库和Agent版本接口响应中的ID；Agent需要version_id，不是agent_id。当前已改为用户直接归属，不再配置RAG_TEST_WORKSPACE_ID。API URL也可改为受信任HTTPS域名，但必须能访问；不要关闭证书验证。

## 2. 数据集格式

LangSmith 中准备名称为RAG的数据集，输入如 `{"question":"资料中介绍的功能是什么？"}`，参考输出如 `{"answer":"参考答案"}`。无证据案例的参考答案按项目实际拒答文案填写。`exact_match`只适合固定文案，开放式问答需要语义正确性、证据忠实度、召回质量等指标，不能仅比较全文相等。

## 3. 手动运行

```powershell
cd E:\Code\YugiTalk\backend
uv run --frozen python evaluate_rag.py
```

此命令会对数据集逐题调用已配置模型并向LangSmith上传问题、答案及来源证据，可能产生费用；仅准备完毕后由你执行。当前未执行真实评测。单并发不代表只调用一次模型，七步流程可能多次调用。测试会保留新建的评测会话，方便核对任务记录；超时请求取消任务，等待审批/澄清则报告未完成。

## 4. 接到自己的函数

```python
from rag_eval_adapter import call_rag

def target_function(inputs: dict) -> dict:
    """调用本项目完整知识库问答任务。"""
    return call_rag(inputs["question"])

# 传函数本身，不能写成 target_function()。
evaluate(target_function, data="RAG", evaluators=[exact_match], max_concurrency=1)
```

独立示例为 `backend/evaluate_rag.py`，适配器为 `backend/rag_eval_adapter.py`。`tests/test_rag.py` 是你正在编辑的草稿；其中不完整的 `result =` 会使pytest收集失败，运行其他测试时先指定测试文件，或将草稿移出tests。

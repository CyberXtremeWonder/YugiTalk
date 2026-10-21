"""检索无次数额度、Token 记账和真实权限边界的回归测试。"""

import json

import pytest

from app.agents.contracts import AgentConfig
from app.agents.graph import run_graph
from app.core.config import Settings
from app.providers.client import HTTPProvider, ModelEvent, ProviderFailure, Reply, ToolCall

pytest_plugins = ["test_identity", "test_knowledge"]


@pytest.mark.parametrize("search_count", [3, 5])
def test_searches_have_no_count_quota(client, storage, monkeypatch, search_count):
    """跨过原有两次和四次限制，后续轮次仍可检索且正常完成 Token 记账。"""
    from test_agents import setup_task
    from test_enhanced_rag import uploaded
    from test_subagents import Script, execute, tool

    from app.rag.pipeline import RagPipeline

    _, _, base, _ = uploaded(client)
    _, _, task, _ = setup_task(
        client, message="比较记忆系统以及适用范围", tools=[], max_tool_calls=0, token_budget=100000,
    )
    searches = []
    original = RagPipeline.execute

    async def search(pipeline, question, resume=None):
        """统计真实进入 RAG 的次数，不把程序拒绝的提案误算为实际检索。"""
        searches.append(question)
        return await original(pipeline, question, resume)

    monkeypatch.setattr(RagPipeline, "execute", search)

    class Worker:
        round = 0

        def stream(self, messages, tools, max_tokens):
            """批量检索后再发起一次检索，不因调用次数撤去工具。"""
            self.round += 1
            if self.round == 1:
                yield ModelEvent("reply", reply=Reply(calls=[
                    ToolCall(id=str(i), name="knowledge_search", arguments={
                        "knowledge_base_id": base, "query": f"Milvus {i}",
                    }) for i in range(search_count)
                ]))
                return
            assert "knowledge_search" in tools and "list_knowledge_bases" in tools
            if self.round == 2:
                yield ModelEvent("reply", reply=tool(
                    "knowledge_search", knowledge_base_id=base, query="Milvus next",
                ))
                return
            results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            assert all(row["ok"] and "stop_search" not in row for row in results)
            yield ModelEvent("text", text="根据现有资料回答，其他部分没有证据。")
            yield ModelEvent("reply", reply=Reply())

    result = execute(client, task, [
        Script([tool("delegate_task", task="记忆系统是什么"), "有限资料结论"], ["delegate_task"]),
        Worker(),
    ])
    assert result["status"] == "succeeded", result
    assert len(searches) == search_count + 1
    assert result["charged_tokens"] > 0


@pytest.mark.parametrize("legacy_limit", [0, 1])
def test_batch_ignores_legacy_tool_count_limits(legacy_limit):
    """历史 Agent 与服务端的调用次数配置不再截断合法工具批次。"""
    class Provider:
        round = 0

        def stream(self, messages, tools, max_tokens):
            self.round += 1
            assert tools == ["text_stats"]
            if self.round == 1:
                yield ModelEvent("reply", reply=Reply(calls=[
                    ToolCall(id=str(i), name="text_stats", arguments={"text": str(i)})
                    for i in range(3)
                ]))
                return
            results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            assert len(results) == 3 and all(row["ok"] for row in results)
            yield ModelEvent("text", text="已完成三项统计。")
            yield ModelEvent("reply", reply=Reply(input_tokens=7, output_tokens=3))

    state = run_graph(
        Settings(app_env="test", max_tool_calls=1), AgentConfig(
            name="test", tools=["text_stats"], max_tool_calls=legacy_limit,
        ), Provider(), [{"role": "user", "content": "统计"}],
        lambda *args: None, lambda text: None, 0,
        tool_executor=lambda *args: {"ok": True},
    )
    assert state["tool_count"] == 3
    assert state["text"] == "已完成三项统计。"


def test_legacy_stop_search_does_not_revoke_authorized_tools():
    """恢复旧检查点时忽略遗留 stop_search，避免继续报没有工具权限。"""
    from app.agents.subagents import SCHEMAS

    class Provider:
        tool_schemas = SCHEMAS
        round = 0

        def stream(self, messages, tools, max_tokens):
            assert "knowledge_search" in tools
            self.round += 1
            if self.round == 1:
                yield ModelEvent("reply", reply=Reply(calls=[
                    ToolCall(id="next", name="knowledge_search", arguments={"query": "记忆系统"}),
                ]))
                return
            yield ModelEvent("text", text="完成。")
            yield ModelEvent("reply", reply=Reply())

    state = run_graph(
        Settings(app_env="test"), AgentConfig(name="test", tools=["knowledge_search"]), Provider(),
        [{"role": "user", "content": "继续", "rag_evidence": {
            "coverage": "none", "sources": [], "stop_search": True,
        }}], lambda *args: None, lambda text: None, 0,
        tool_executor=lambda *args: {"ok": True, "coverage": "none", "sources": []},
    )
    assert state["tool_count"] == 1


def test_bounded_deepseek_answer_uses_budget_for_visible_text():
    """只在有界回答模式关闭内部思考，普通请求保留原模型行为。"""
    provider = HTTPProvider(Settings(app_env="test"), "openai_compatible", "deepseek-flash")
    assert "thinking" not in provider.request_body([], [], 1024)
    provider.bounded_answer = True
    body = provider.request_body([], [], 1024)
    assert body["thinking"] == {"type": "disabled"} and body["max_tokens"] == 1024


def test_permission_failure_is_never_downgraded_to_budget_result():
    """取消次数额度后，真实权限撤销仍必须立即终止。"""
    class Provider:
        def stream(self, *args):
            """提出一次合法工具调用。"""
            yield ModelEvent("reply", reply=Reply(calls=[
                ToolCall(id="one", name="text_stats", arguments={"text": "a"}),
            ]))

    def revoked(*args):
        """模拟执行前权限已被撤销。"""
        raise ProviderFailure("permission_revoked")

    with pytest.raises(ProviderFailure, match="permission_revoked"):
        run_graph(
            Settings(app_env="test"), AgentConfig(name="test", tools=["text_stats"]), Provider(),
            [{"role": "user", "content": "统计"}], lambda *args: None, lambda text: None, 0,
            tool_executor=revoked,
        )

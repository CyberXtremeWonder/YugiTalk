"""多路检索上下文容量、完整证据保留及恢复回归，不调用真实模型。"""

import copy
import json

import pytest

from app.agents.answer_policy import answer_instruction
from app.agents.contracts import AgentConfig
from app.agents.graph import run_graph
from app.core.config import Settings
from app.guardrails.prompts import protect_messages
from app.providers.client import ModelEvent, ProviderFailure, Reply, ToolCall


@pytest.mark.parametrize("resume", [False, True])
def test_four_web_searches_and_knowledge_result_fit_without_clipping(resume):
    """截图中的五次调用可继续合成，恢复时不重放已完成搜索或删减资料。"""
    calls = [
        ToolCall(id=f"web-{i}", name="web_search", arguments={"query": str(i)}) for i in range(4)
    ]
    calls.append(ToolCall(id="knowledge", name="knowledge_search", arguments={"query": "q"}))
    results = {
        call.id: {
            "ok": True,
            "source": "Tavily",
            "results": [
                {
                    "title": f"来源 {i}",
                    "url": f"https://example.org/{call.id}/{i}",
                    "content": "搜索摘要" * 40,
                    "raw_content": "完整原文" * 160,
                }
                for i in range(5)
            ],
        }
        for call in calls[:4]
    }
    results["knowledge"] = {"ok": True, "sources": [], "coverage": "none"}
    settings = Settings(app_env="test")
    config = AgentConfig(name="研究", tools=["web_search", "knowledge_search"])
    executed, checkpoints = [], []

    class Provider:
        """第一轮提出五次检索，第二轮逐项核对完整来源。"""

        tool_schemas = {name: {"name": name, "parameters": {}} for name in config.tools}
        guard = staticmethod(lambda: None)

        def stream(self, messages, tools, max_tokens):
            """已有工具消息时验证原文及来源，不接受只保留摘要的变通。"""
            tool_messages = [message for message in messages if message["role"] == "tool"]
            if not tool_messages:
                yield ModelEvent("reply", reply=Reply(calls=calls))
                return
            assert len(tool_messages) == 5
            assert 32768 < len(json.dumps(messages, ensure_ascii=False).encode()) < 131072
            for message, call in zip(tool_messages, calls, strict=True):
                assert json.loads(message["content"]) == results[call.id]
            if resume and len(checkpoints) and checkpoints[-1][0] == "model" and not resumed:
                raise ProviderFailure("model_transient")
            yield ModelEvent("text", text="已根据检索资料完成整理。")
            yield ModelEvent("reply", reply=Reply())

    def execute(call, step):
        """统计实际工具调用，恢复后不允许再调用一次。"""
        executed.append(call.id)
        return copy.deepcopy(results[call.id])

    def save(state, node):
        """保存真实图边界，包含已完成工具游标及原始结果。"""
        checkpoints.append((node, copy.deepcopy(state)))

    resumed = False
    arguments = (
        settings,
        config,
        Provider(),
        [{"role": "user", "content": "研究资料"}],
        lambda *args: None,
        lambda text: None,
        0,
    )
    if resume:
        with pytest.raises(ProviderFailure, match="model_transient"):
            run_graph(*arguments, save=save, tool_executor=execute)
        resumed = True
        node, state = checkpoints[-1]
        result = run_graph(*arguments, resume=state, node=node, save=save, tool_executor=execute)
    else:
        result = run_graph(*arguments, save=save, tool_executor=execute)
    assert result["text"] == "已根据检索资料完成整理。"
    assert executed == [call.id for call in calls]


@pytest.mark.parametrize("large_schema", [False, True])
def test_configured_limit_blocks_utf8_input_or_tool_schema_before_model(large_schema, monkeypatch):
    """字节容量包含中文及 Schema，超限前不调用供应商或预留调用费用。"""
    details, reserves = [], []
    monkeypatch.setattr("app.agents.graph.trace_details", lambda **values: details.append(values))

    class Provider:
        """任何实际调用都表示容量保护失效。"""

        tool_schemas = {"text_stats": {"description": "x" * 9000}}

        def stream(self, *args):
            pytest.fail("超限请求不应发送")

    with pytest.raises(ProviderFailure, match="context_limit"):
        run_graph(
            Settings(app_env="test", model_context_limit_bytes=8192),
            AgentConfig(name="容量测试", tools=["text_stats"] if large_schema else []),
            Provider(),
            [{"role": "user", "content": "问题" if large_schema else "中" * 3000}],
            lambda kind, payload, reserve: reserves.append(reserve),
            lambda text: None,
            0,
        )
    assert not any(reserves)
    assert details[-1]["context_bytes"] > details[-1]["context_limit_bytes"] == 8192


def test_evidence_instruction_is_counted_in_actual_request_capacity():
    """在原输入将将未超限时，追加的证据约束也必须计入容量。"""
    messages = [
        {
            "role": "user",
            "content": "",
            "rag_evidence": {
                "coverage": "partial",
                "sources": [{"evidence_id": "E-one"}],
            },
        }
    ]
    protected = protect_messages(messages)
    plain = [{k: v for k, v in m.items() if k != "rag_evidence"} for m in protected]
    base_size = len(json.dumps({"messages": plain, "tools": []}, ensure_ascii=False).encode())
    messages[0]["content"] = "x" * (4096 - base_size - 10)
    assert len(answer_instruction({"coverage": "partial"}).encode()) > 10

    class Provider:
        """追加指令后超过上限，不应调用模型。"""

        def stream(self, *args):
            pytest.fail("应在包含证据约束后拒绝超限请求")

    with pytest.raises(ProviderFailure, match="context_limit"):
        run_graph(
            Settings(app_env="test", model_context_limit_bytes=4096),
            AgentConfig(name="容量测试", tools=[]),
            Provider(),
            messages,
            lambda *args: None,
            lambda text: None,
            0,
        )


def test_context_failure_can_retry_saved_checkpoint_after_capacity_update(client, monkeypatch):
    """调整容量后原任务可重试，已提交工具结果无需再次调用。"""
    from uuid import uuid4

    from sqlalchemy.orm import Session
    from test_agents import setup_task
    from test_subagents import WORKER_TOOLS, Script

    from app.tasks.execution import Execution
    from app.tasks.models import Run

    headers, _, task, _ = setup_task(client, "整理已有资料", tools=["text_stats"])
    engine = client.app.state.database
    with Session(engine) as db:
        row = db.get(Run, task["id"])
        row.checkpoint = {
            "node": "model",
            "state": {
                "messages": [
                    {"role": "user", "content": "整理已有资料"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "saved",
                                "type": "function",
                                "function": {
                                    "name": "text_stats",
                                    "arguments": '{"text":"a"}',
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "saved",
                        "content": json.dumps({"payload": "x" * 40000}),
                    },
                ],
                "text": "",
                "step": 1,
                "calls": [],
                "tool_count": 1,
                "tool_cursor": 1,
                "repetitions": {},
                "input_tokens": 0,
                "output_tokens": 0,
            },
        }
        db.commit()
    settings = client.app.state.settings
    settings.task_budget_enabled = False
    settings.model_context_limit_bytes = 32768
    monkeypatch.setattr(Execution, "tool", lambda *args: pytest.fail("不应重放已提交工具"))
    Execution(engine, settings, task["id"]).execute(lambda *args: Script([], WORKER_TOOLS))
    failed = client.get(f"/api/v1/runs/{task['id']}").json()
    assert failed["error_code"] == "context_limit" and failed["retryable"]
    settings.model_context_limit_bytes = 131072
    headers["Idempotency-Key"] = str(uuid4())
    assert client.post(f"/api/v1/runs/{task['id']}/retry", headers=headers).status_code == 202

    def answer(messages):
        """恢复后原文没有因容量调整被裁剪。"""
        assert json.loads(messages[-1]["content"])["payload"] == "x" * 40000
        return "已完成整理。"

    Execution(engine, settings, task["id"]).execute(lambda *args: Script([answer], WORKER_TOOLS))
    assert client.get(f"/api/v1/runs/{task['id']}").json()["status"] == "succeeded"


pytest_plugins = ["test_identity"]

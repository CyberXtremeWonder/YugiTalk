"""部分证据、生成预算、流式事件容量与断线续传的边界回归。"""

import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_agents import setup_task

from app.agents.answer_policy import answer_instruction, evidence_policy, validate_references
from app.agents.contracts import AgentConfig
from app.agents.graph import run_graph
from app.agents.streaming import TextStream
from app.core.config import Settings
from app.providers.client import ModelEvent, ProviderFailure, Reply
from app.rag.contracts import EvidenceGrade
from app.rag.pipeline import RagPipeline
from app.tasks.execution import Execution
from app.tasks.models import RunEvent

pytest_plugins = ["test_identity", "test_knowledge"]


def test_partial_evidence_survives_grading(client):
    """复现强相关但不充分的资料：旧评分器要求改写时也应保留证据，而非清空。"""
    class Model:
        async def call(self, schema, purpose, payload, deadline):
            """若发生多余改写，本测试在此直接失败。"""
            assert purpose == "grade"
            return EvidenceGrade(
                relevance="strong", answerability="partial", ambiguity="none", route="rewrite"
            )

    sources = [{"evidence_id": "E-one", "content": "记忆系统保存和检索历史信息。"}]
    state = RagPipeline(
        client.app.state.database,
        client.app.state.settings.model_copy(update={"model_provider": "openai_compatible"}),
        "space", "owner", "base", lambda *args: None, lambda state: None, Model(),
    ).run("", {
        "phase": "grade", "question": "定义与局限？", "sources": sources,
        "rewrite_count": 0, "grade_count": 0, "clarification_count": 0, "trace": [],
    })
    assert state["route"] == "partial_answer" and state["sources"]
    assert state["rewrite_count"] == 0 and state["grade_count"] == 1


def test_empty_result_does_not_erase_other_base_sources():
    """跨库空结果不得清除先前命中的证据，引用校验始终绑定实际来源集合。"""
    messages = [{"role": "tool", "content": json.dumps(value)} for value in [
        {"coverage": "partial", "sources": [{"evidence_id": "E-real"}]},
        {"coverage": "none", "sources": []},
    ]]
    policy = evidence_policy(messages)
    assert policy["coverage"] == "partial"
    validate_references("依据 E-real", policy)
    for invalid in ("依据E-fake", "依据 [S1]"):
        with pytest.raises(ProviderFailure, match="invalid_citation"):
            validate_references(invalid, policy)
    assert "本次检索无有效证据" in answer_instruction({"coverage": "none", "sources": []})


def test_partial_sources_reach_worker_router_and_saved_message(client, storage, monkeypatch):
    """从真实索引到主助手和消息落库，验证部分覆盖不会在委派边界丢失。"""
    from test_enhanced_rag import uploaded
    from test_subagents import Script, execute, tool

    _, _, base, _ = uploaded(client)
    _, body, task, _ = setup_task(client, tools=[], token_budget=100000)
    monkeypatch.setattr("app.rag.pipeline.offline_grade", lambda *args: EvidenceGrade(
        relevance="strong", answerability="partial", ambiguity="none", route="rewrite",
    ))

    def worker_answer(messages):
        """检索工具结果保留来源，生成系统约束切换为部分回答。"""
        result = json.loads(messages[-1]["content"])
        assert result["coverage"] == "partial" and result["sources"]
        assert "资料仅部分支持" in messages[0]["content"]
        return "Milvus 保存向量；局限未被资料覆盖。"

    def router_answer(messages):
        """主助手收到结构化覆盖程度，而非只有子助手总结。"""
        result = json.loads(messages[-1]["content"])
        assert result["coverage"] == "partial" and result["sources"]
        assert "资料仅部分支持" in messages[0]["content"]
        return "资料表明 Milvus 保存向量，其他局限暂无依据。"

    result = execute(client, task, [
        Script(
            [tool("delegate_task", task="Milvus 的用途和局限"), router_answer], ["delegate_task"],
        ),
        Script([
            tool("knowledge_search", knowledge_base_id=base, query="Milvus"), worker_answer,
        ], ["list_knowledge_bases", "knowledge_search"]),
    ])
    assert result["status"] == "succeeded", result
    messages = client.get(f"/api/v1/conversations/{body['conversation_id']}/messages").json()
    assert messages["items"][-1]["sources"]


@pytest.mark.parametrize("fail", [False, True])
def test_fragmented_answer_is_batched_and_tail_survives_error(fail):
    """两千个碎片不再产生两千个事件；异常时最后不足一批的文字仍交付。"""
    answer = "".join(chr(0x4E00 + index) for index in range(2033))
    events, collected, limits = [], [], []

    class Provider:
        def stream(self, messages, tools, max_tokens):
            """模拟高速单字流，检查实际发送给模型的部分回答预算及指令。"""
            limits.append(max_tokens)
            assert "资料仅部分支持" in messages[0]["content"]
            assert not any("rag_evidence" in message for message in messages)
            for character in answer:
                yield ModelEvent("text", text=character)
            if fail:
                raise ProviderFailure("model_incomplete")
            yield ModelEvent("reply", reply=Reply())

    def execute():
        """直接运行生成图，避免网络和数据库速度影响合并阈值验证。"""
        return run_graph(
            Settings(app_env="test"), AgentConfig(name="test", tools=[]), Provider(),
            [{"role": "user", "content": "定义？", "rag_evidence": {
                "coverage": "partial", "sources": [{"evidence_id": "E-real"}],
            }}], lambda kind, payload, reserve: events.append((kind, payload)),
            collected.append, 0,
        )

    if fail:
        with pytest.raises(ProviderFailure, match="model_incomplete"):
            execute()
    else:
        assert execute()["text"] == answer
    assert "".join(collected) == answer and limits == [1024]
    deltas = [payload["text"] for kind, payload in events if kind == "text_delta"]
    assert "".join(deltas) == answer and len(deltas) < 30


def test_repetition_gate_handles_fragment_boundaries():
    """重复段落跨片到达时仍被拦截；前两次出现允许正常输出。"""
    stream = TextStream(lambda text: None)
    paragraph = "".join(chr(0x4E00 + index) for index in range(100))
    with pytest.raises(ProviderFailure, match="model_repetition"):
        for character in paragraph * 3:
            stream.add(character)


def test_slow_stream_flushes_without_waiting_for_full_batch(monkeypatch):
    """少量文字达到时间阈值即发布，结束时重复 flush 不会再发相同内容。"""
    clock = [0.0]
    monkeypatch.setattr("app.agents.streaming.time.monotonic", lambda: clock[0])
    output = []
    stream = TextStream(output.append)
    stream.add("首")
    assert not output
    clock[0] = 0.16
    stream.add("句")
    stream.flush()
    assert output == ["首句"]


def test_filtered_events_keep_terminal_and_replay_cursor(client):
    """选择文字订阅后，终态仍到达；按原序号续传不会重复正文。"""
    _, _, task, _ = setup_task(client)
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute()
    response = client.get(task["events_url"] + "?types=text_delta")
    assert response.status_code == 200
    assert "event: tool_status" not in response.text
    assert "event: completed" in response.text
    with Session(client.app.state.database) as db:
        last = db.scalar(select(RunEvent).where(
            RunEvent.run_id == task["id"], RunEvent.kind == "text_delta",
        ).order_by(RunEvent.sequence.desc()))
    replay = client.get(task["events_url"] + "?types=text_delta", headers={
        "Last-Event-ID": f"{task['id']}:{last.sequence}",
    })
    assert "event: text_delta" not in replay.text and "event: completed" in replay.text
    assert client.get(task["events_url"] + "?types=unknown").status_code == 422


def test_event_capacity_failure_is_explicit_and_replayable(client):
    """故意用小容量触顶，确认保留正文、写入截断终态，并能从断点补读错误。"""
    _, _, task, _ = setup_task(client)
    settings = client.app.state.settings
    settings.run_event_limit = 20

    class Provider:
        def stream(self, messages, tools, max_tokens):
            """不依赖外部模型，用不重复的正文确定性超过二十条合并事件。"""
            for index in range(4000):
                yield ModelEvent("text", text=chr(0x4E00 + index))
            yield ModelEvent("reply", reply=Reply())

    Execution(client.app.state.database, settings, task["id"]).execute(lambda *args: Provider())
    result = client.get(f"/api/v1/runs/{task['id']}").json()
    assert result["status"] == "failed" and result["error_code"] == "event_limit"
    assert result["event_sequence"] == 20
    with Session(client.app.state.database) as db:
        terminal = db.scalar(select(RunEvent).where(
            RunEvent.run_id == task["id"], RunEvent.kind == "error",
        ))
        assert terminal.payload["truncated"] is True
    replay = client.get(task["events_url"], headers={"Last-Event-ID": f"{task['id']}:19"})
    assert "event: error" in replay.text and "event_limit" in replay.text

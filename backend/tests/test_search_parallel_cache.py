"""跨库工具并行、任务内查询复用、版本失效与失败恢复的回归。"""

import asyncio
import json

import pytest
from sqlalchemy.orm import Session
from test_agents import setup_task
from test_enhanced_rag import uploaded
from test_reliability import expire_lease, recover
from test_subagents import Script, execute, tool

from app.providers.client import ProviderFailure, Reply, ToolCall
from app.rag.models import Document
from app.rag.pipeline import RagPipeline
from app.tasks.execution import Execution
from app.tasks.models import Run

pytest_plugins = ["test_identity", "test_knowledge"]
TOOLS = ["list_knowledge_bases", "knowledge_search", "research_report", "export_document"]


def searches(base, queries):
    return Reply(calls=[
        ToolCall(id=str(i), name="knowledge_search", arguments={
            "knowledge_base_id": base, "query": query,
        }) for i, query in enumerate(queries)
    ])


@pytest.mark.parametrize("separator", ["duplicate", "catalog"])
def test_independent_search_is_not_blocked_by_duplicate_or_catalog(
    client, storage, monkeypatch, separator,
):
    _, _, base, _ = uploaded(client)
    _, _, task, _ = setup_task(client, "Milvus 是什么", tools=[], token_budget=100000)
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        run.snapshot = {**run.snapshot, "rag_config": {
            **run.snapshot["rag_config"], "rag_parallel_queries": 2,
        }}
        db.commit()
    started, queries = None, []

    async def independent(pipeline, question, resume):
        nonlocal started
        if started is None:
            started = asyncio.Event()
        queries.append(question)
        if question == "Milvus A":
            # A 必须与 B 同时在执行；重复 A 或目录不能挡住 B 的并发名额。
            await asyncio.wait_for(started.wait(), timeout=1)
        else:
            started.set()
        return {"phase": "done", "sources": [], "route": "no_knowledge"}

    monkeypatch.setattr(RagPipeline, "execute", independent)
    batch = searches(base, ["Milvus A", "Milvus A", "Milvus B"])
    if separator == "catalog":
        batch.calls[1] = ToolCall(id="1", name="list_knowledge_bases", arguments={})

    def answer(messages):
        results = [m for m in messages if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in results] == ["0", "1", "2"]
        middle = json.loads(results[1]["content"])
        if separator == "duplicate":
            assert middle["reused"] and middle["new_evidence_count"] == 0
        else:
            assert any(item["id"] == base for item in middle["items"])
        return "没有找到充分证据。"

    result = execute(client, task, [Script([batch, answer], TOOLS)])
    assert result["status"] == "succeeded", result
    assert queries == ["Milvus A", "Milvus B"]


@pytest.mark.parametrize("boundary", ["text_stats", "unauthorized_catalog"])
def test_search_batch_respects_tool_boundary_and_authorization(boundary):
    from app.agents.contracts import AgentConfig
    from app.agents.graph import run_graph
    from app.agents.subagents import SCHEMAS
    from app.core.config import Settings
    from app.tools.registry import TOOL_SCHEMAS

    calls = searches("00000000-0000-0000-0000-000000000001", ["A", "B"])
    middle = ToolCall(id="middle", name="text_stats", arguments={"text": "abc"})
    if boundary == "unauthorized_catalog":
        middle = ToolCall(id="middle", name="list_knowledge_bases", arguments={})
    calls.calls.insert(1, middle)
    executed, batches = [], []
    provider = Script([calls, "完成"], ["knowledge_search", "text_stats"])
    provider.tool_schemas = {**TOOL_SCHEMAS, **SCHEMAS}

    def execute_one(call, step):
        executed.append(call.id)
        return {"ok": True}

    def batch(pending, step):
        batches.append([call.id for call in pending])
        return [execute_one(call, step) for call in pending]

    def run():
        return run_graph(
            Settings(_env_file=None, app_env="test"),
            AgentConfig(name="边界", tools=["knowledge_search", "text_stats"]), provider,
            [{"role": "user", "content": "执行查询"}], lambda *args: None, lambda text: None, 0,
            tool_executor=execute_one, tool_batch_executor=batch,
        )

    if boundary == "unauthorized_catalog":
        with pytest.raises(ProviderFailure, match="tool_not_authorized"):
            run()
        assert not executed and not batches
    else:
        run()
        assert executed == ["0", "middle", "1"] and not batches


@pytest.mark.parametrize("limit", [1, 2, 4])
def test_parallel_limit_preserves_order_sources_and_accounting(client, storage, monkeypatch, limit):
    _, _, base, _ = uploaded(client)
    _, _, task, _ = setup_task(client, "Milvus 是什么", tools=[], token_budget=100000)
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        run.snapshot = {**run.snapshot, "rag_config": {
            **run.snapshot["rag_config"], "rag_parallel_queries": limit,
        }}
        db.commit()
    active, peak, queries, reservations = 0, 0, [], []
    original, update = RagPipeline.execute, Execution.update

    async def delayed(pipeline, question, resume):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        queries.append(question)
        try:
            # 显式让出执行，验证多条完整 RAG 流程能够重叠，而非依赖墙钟时间。
            await asyncio.sleep(0)
            return await original(pipeline, question, resume)
        finally:
            active -= 1

    def reserve(execution, kind=None, payload=None, cost=0):
        reservations.append(cost)
        return update(execution, kind, payload, cost)

    monkeypatch.setattr(RagPipeline, "execute", delayed)
    monkeypatch.setattr(Execution, "update", reserve)

    def answer(messages):
        results = [m for m in messages if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in results] == ["0", "1", "2", "3"]
        assert all(json.loads(m["content"])["sources"] for m in results)
        return "Milvus 保存向量。"

    result = execute(client, task, [Script([
        searches(base, [f"Milvus {i}" for i in range(4)]), answer,
    ], TOOLS)])
    assert result["status"] == "succeeded", result
    assert peak == limit and active == 0 and len(queries) == 4
    assert result["charged_tokens"] == sum(reservations)


def test_identical_queries_share_inflight_and_saved_results(client, storage, monkeypatch):
    _, _, base, _ = uploaded(client)
    _, _, task, _ = setup_task(client, "Milvus 是什么", tools=[], token_budget=100000)
    queries = []
    original = RagPipeline.execute

    async def counted(pipeline, question, resume):
        queries.append(question)
        await asyncio.sleep(0)
        return await original(pipeline, question, resume)

    monkeypatch.setattr(RagPipeline, "execute", counted)

    def repeat(messages):
        rows = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        assert len(rows) == 3 and all(row["sources"] for row in rows)
        assert sum(bool(row.get("reused")) for row in rows) == 2
        return tool("knowledge_search", knowledge_base_id=base, query="Milvus")

    def answer(messages):
        result = json.loads(messages[-1]["content"])
        assert result["reused"] and result["new_evidence_count"] == 0
        return "已依据已有证据回答。"

    result = execute(client, task, [Script([
        searches(base, ["Milvus", "Milvus ", "Milvus"]), repeat, answer,
    ], TOOLS)])
    assert result["status"] == "succeeded", result
    assert queries == ["Milvus"]


def test_empty_cache_invalidates_when_document_version_changes(client, storage, monkeypatch):
    _, _, base, document = uploaded(client)
    _, _, task, _ = setup_task(client, "查找资料", tools=[], token_budget=100000)
    queries = []

    async def empty(pipeline, question, resume):
        queries.append(question)
        return {"phase": "done", "sources": [], "route": "no_knowledge"}

    monkeypatch.setattr(RagPipeline, "execute", empty)

    def change_version(messages):
        with Session(client.app.state.database) as db:
            db.get(Document, document["id"]).index_version += 1
            db.commit()
        return tool("knowledge_search", knowledge_base_id=base, query="不存在的主题")

    result = execute(client, task, [Script([
        tool("knowledge_search", knowledge_base_id=base, query="不存在的主题"),
        change_version, "没有找到证据。",
    ], TOOLS)])
    assert result["status"] == "succeeded", result
    assert len(queries) == 2


def test_batch_failure_cancels_siblings_and_resume_reuses_completed_results(
    client, storage, monkeypatch,
):
    _, _, base, _ = uploaded(client)
    _, _, task, _ = setup_task(client, "查找资料", tools=[], token_budget=100000)
    queries, stopped = [], []
    completed = None

    async def interrupted(pipeline, question, resume):
        nonlocal completed
        if completed is None:
            completed = asyncio.Event()
        queries.append(question)
        if question == "success":
            completed.set()
            return {"phase": "done", "sources": [], "route": "no_knowledge"}
        if question == "failure":
            await completed.wait()
            # 让先完成的查询提交结果，再模拟另一条查询中断。
            await asyncio.sleep(0)
            raise ProviderFailure("model_timeout_or_network")
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(question)

    monkeypatch.setattr(RagPipeline, "execute", interrupted)
    result = execute(client, task, [Script([
        searches(base, ["success", "failure", "pending"]),
    ], TOOLS)])
    assert result["error_code"] == "model_timeout_or_network"
    assert stopped == ["pending"]
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        records = run.checkpoint["subagents"]["direct"]["tools"].values()
        assert sum("result" in item for item in records) == 1
        # 重试接口的权限/预算校验在 reliability 测试覆盖；此处模拟可恢复的执行领取。
        run.status = "running"
        db.commit()
    expire_lease(client, task["id"])
    recover(client)

    async def finish(pipeline, question, resume):
        queries.append(question)
        return {"phase": "done", "sources": [], "route": "no_knowledge"}

    monkeypatch.setattr(RagPipeline, "execute", finish)
    result = execute(client, task, [Script(["已恢复。"], TOOLS)])
    assert result["status"] == "succeeded", result
    assert queries.count("success") == 1
    assert queries.count("failure") == 2 and queries.count("pending") == 2


def test_cached_source_removal_is_not_bypassed(client, storage, monkeypatch):
    _, _, base, document = uploaded(client)
    _, _, task, _ = setup_task(client, "Milvus 是什么", tools=[], token_budget=100000)

    def remove(messages):
        with Session(client.app.state.database) as db:
            db.get(Document, document["id"]).status = "deleted"
            db.commit()
        return tool("knowledge_search", knowledge_base_id=base, query="Milvus")

    result = execute(client, task, [Script([
        tool("knowledge_search", knowledge_base_id=base, query="Milvus"), remove,
    ], TOOLS)])
    assert result["error_code"] == "knowledge_source_removed"

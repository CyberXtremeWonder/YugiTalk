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
TOOLS = ["list_knowledge_bases", "knowledge_search"]


def searches(base, queries):
    return Reply(calls=[
        ToolCall(id=str(i), name="knowledge_search", arguments={
            "knowledge_base_id": base, "query": query,
        }) for i, query in enumerate(queries)
    ])


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

"""简单问答减少模型往返，保留来源、流式输出、目录隔离与检查点恢复。"""

import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_agents import setup_task
from test_enhanced_rag import uploaded
from test_reliability import expire_lease, recover
from test_subagents import Script, execute, tool

from app.identity.models import User
from app.providers.client import ModelEvent, Reply
from app.rag.models import KnowledgeBase
from app.tasks.execution import Execution
from app.tasks.ledger_models import ToolExecution
from app.tasks.models import Run, RunEvent

pytest_plugins = ["test_identity", "test_knowledge"]


def catalog(messages):
    """目录始终以低优先级用户数据提供。"""
    for message in messages:
        if message["role"] != "user":
            continue
        try:
            value = json.loads(message["content"])
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("context_kind") == "knowledge_catalog":
            return value["data"]
    raise AssertionError("missing knowledge catalog")


def test_simple_search_uses_one_provider_two_calls_and_streams(client, storage):
    _, _, base, _ = uploaded(client)
    _, body, task, _ = setup_task(client, "Milvus 是什么", tools=[], token_budget=100000)
    answer = "Milvus 保存向量。" + "".join(chr(0x4E00 + i) for i in range(130))
    calls, providers = [], []

    class Provider:
        def stream(self, messages, tools, max_tokens):
            calls.append(list(tools))
            assert "delegate_task" not in tools and "knowledge_search" in tools
            if len(calls) == 1:
                assert base in {row["id"] for row in catalog(messages)["items"]}
                assert messages[-1]["content"] == "Milvus 是什么"
                yield ModelEvent("reply", reply=tool(
                    "knowledge_search", knowledge_base_id=base, query="Milvus",
                ))
                return
            assert json.loads(messages[-1]["content"])["sources"]
            yield ModelEvent("text", text=answer)
            # 模型尚未返回最终 Reply 时，正文已进入公开事件，不再等待主助手汇总。
            with Session(client.app.state.database) as db:
                assert db.scalar(select(RunEvent).where(
                    RunEvent.run_id == task["id"], RunEvent.kind == "text_delta",
                )) is not None
            yield ModelEvent("reply", reply=Reply(input_tokens=10, output_tokens=20))

    def factory(*args):
        providers.append(Provider())
        return providers[-1]

    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(factory)
    result = client.get(f"/api/v1/runs/{task['id']}").json()
    assert result["status"] == "succeeded", result
    assert len(providers) == 1 and len(calls) == 2
    rows = client.get(f"/api/v1/conversations/{body['conversation_id']}/messages").json()
    assert rows["items"][-1]["content"] == answer
    assert rows["items"][-1]["sources"]


def test_direct_resume_reuses_completed_business_tool(client):
    _, _, task, _ = setup_task(client, "统计 abc", tools=["text_stats"], token_budget=100000)

    class Crash(BaseException):
        pass

    def crash(messages):
        raise Crash()

    with pytest.raises(Crash):
        execute(client, task, [Script([
            tool("text_stats", text="abc"), crash,
        ], ["text_stats", "list_knowledge_bases", "knowledge_search"])])
    expire_lease(client, task["id"])
    recover(client)
    result = execute(client, task, [Script([
        "统计完成。",
    ], ["text_stats", "list_knowledge_bases", "knowledge_search"])])
    assert result["status"] == "succeeded", result
    with Session(client.app.state.database) as db:
        assert len(list(db.scalars(select(ToolExecution)))) == 1
        messages = db.get(Run, task["id"]).checkpoint["state"]["messages"]
        assert sum('"context_kind": "knowledge_catalog"' in m["content"] for m in messages) == 1


def test_direct_catalog_is_private_and_paginated(client):
    _, _, task, _ = setup_task(client, "查找项目资料", tools=[], token_budget=100000)
    with Session(client.app.state.database) as db:
        owner = db.get(Run, task["id"]).owner_id
        other = db.scalar(select(User).where(User.id != owner))
        db.add(KnowledgeBase(account_id=other.id, name="其他用户的私有库"))
        for i in range(21):
            db.add(KnowledgeBase(account_id=owner, name=f"项目资料 {i}"))
        db.commit()
    seen = set()

    def next_page(messages):
        page = catalog(messages)
        assert len(page["items"]) == 20 and page["next_offset"] == 20
        assert all(row["name"].startswith("项目资料") for row in page["items"])
        seen.update(row["id"] for row in page["items"])
        return tool("list_knowledge_bases", offset=page["next_offset"])

    def answer(messages):
        page = json.loads(messages[-1]["content"])
        assert len(page["items"]) == 1 and page["next_offset"] is None
        assert page["items"][0]["id"] not in seen
        assert page["items"][0]["name"].startswith("项目资料")
        return "目录读取完成。"

    result = execute(client, task, [Script([
        next_page, answer,
    ], ["list_knowledge_bases", "knowledge_search"])])
    assert result["status"] == "succeeded", result


def test_direct_cannot_delegate_or_expand_permissions(client):
    _, _, task, _ = setup_task(client, "执行任务", tools=[], token_budget=100000)
    result = execute(client, task, [Script([
        tool("delegate_task", task="越权委派"),
    ], ["list_knowledge_bases", "knowledge_search"])])
    assert result["error_code"] == "tool_not_authorized"


def test_complex_worker_also_receives_catalog(client):
    _, _, task, _ = setup_task(client, "比较两个项目", tools=[], token_budget=100000)

    def answer(messages):
        assert catalog(messages) == {"items": [], "next_offset": None}
        return "没有项目资料。"

    result = execute(client, task, [
        Script([tool("delegate_task", task="比较两个项目"), "无法比较。"], ["delegate_task"]),
        Script([answer], ["list_knowledge_bases", "knowledge_search"]),
    ])
    assert result["status"] == "succeeded", result


def test_legacy_task_keeps_router_path(client):
    """没有 direct_mode 的旧任务不因升级而切换执行路径。"""
    _, _, task, _ = setup_task(client, "简单问题", tools=[], token_budget=100000)
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        run.snapshot = {key: value for key, value in run.snapshot.items() if key != "direct_mode"}
        db.commit()
    result = execute(client, task, [
        Script([tool("delegate_task", task="简单问题"), "最终答案。"], ["delegate_task"]),
        Script(["子结果。"], ["list_knowledge_bases", "knowledge_search"]),
    ])
    assert result["status"] == "succeeded", result


def test_direct_refuses_removed_source_before_streaming(client, storage):
    from app.rag.models import Document

    _, _, base, document = uploaded(client)
    _, _, task, _ = setup_task(client, "Milvus 是什么", tools=[], token_budget=100000)

    def remove_source(messages):
        with Session(client.app.state.database) as db:
            db.get(Document, document["id"]).status = "deleted"
            db.commit()
        return "不得公开的答案"

    result = execute(client, task, [Script([
        tool("knowledge_search", knowledge_base_id=base, query="Milvus"), remove_source,
    ], ["list_knowledge_bases", "knowledge_search"])])
    assert result["error_code"] == "knowledge_source_removed"
    assert "不得公开的答案" not in client.get(task["events_url"]).text

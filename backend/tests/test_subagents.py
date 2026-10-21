"""Router 委派、工具隔离、自动 RAG 与父子检查点恢复的行为回归。"""

import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_agents import setup_task as create_task
from test_enhanced_rag import uploaded
from test_identity import login
from test_reliability import expire_lease, recover

from app.providers.client import ModelEvent, Reply, ToolCall
from app.tasks.execution import Execution
from app.tasks.ledger_models import ToolExecution
from app.tasks.models import Run

pytest_plugins = ["test_identity", "test_knowledge"]


def setup_task(client, message="比较两个任务的结果", **config):
    """委派路径测试使用明确多意图问题，简单路径另行覆盖。"""
    return create_task(client, message, **config)


def tool(name, **arguments):
    return Reply(calls=[ToolCall(id="same-id", name=name, arguments=arguments)])


class Script:
    def __init__(self, actions, allowed):
        self.actions = iter(actions)
        self.allowed = set(allowed)
        self.guard = lambda: None

    def stream(self, messages, tools, max_tokens):
        self.guard()
        # 预算收尾会撤去部分工具，任何轮次都不能暴露许可集合以外的工具。
        assert set(tools) <= self.allowed
        action = next(self.actions)
        if callable(action):
            action = action(messages)
        if isinstance(action, Reply):
            yield ModelEvent("reply", reply=action)
        else:
            yield ModelEvent("text", text=action)
            yield ModelEvent("reply", reply=Reply(input_tokens=2, output_tokens=3))


def execute(client, task, providers):
    instances = iter(providers)
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(
        lambda *args: next(instances)
    )
    return client.get(f"/api/v1/runs/{task['id']}").json()


WORKER_TOOLS = ["text_stats", "list_knowledge_bases", "knowledge_search"]


def test_router_delegates_tools_and_keeps_child_text_private(client):
    _, body, task, _ = setup_task(client, tools=["text_stats"], token_budget=100000)

    def summary(messages):
        result = json.loads(messages[-1]["content"])
        assert result == {"ok": True, "summary": "子助手内部结果"}
        return "最终答案"

    result = execute(
        client,
        task,
        [
            Script([tool("delegate_task", task="统计 abc"), summary], ["delegate_task"]),
            Script([tool("text_stats", text="abc"), "子助手内部结果"], WORKER_TOOLS),
        ],
    )
    assert result["status"] == "succeeded", result
    events = client.get(task["events_url"]).text
    assert "子助手内部结果" not in events and "最终答案" in events
    assert '"stage":"subagent"' in events.replace(" ", "")
    messages = client.get(f"/api/v1/conversations/{body['conversation_id']}/messages").json()
    assert messages["items"][-1]["content"] == "最终答案"
    with Session(client.app.state.database) as db:
        assert db.scalar(select(ToolExecution)).tool == "text_stats"


@pytest.mark.parametrize("target", ["router", "worker"])
def test_roles_cannot_expand_tool_permissions(client, target):
    _, _, task, _ = setup_task(client, tools=["text_stats"], token_budget=100000)
    providers = (
        [Script([tool("text_stats", text="abc")], ["delegate_task"])]
        if target == "router"
        else [
            Script([tool("delegate_task", task="执行")], ["delegate_task"]),
            Script([tool("delegate_task", task="递归委派")], WORKER_TOOLS),
        ]
    )
    result = execute(client, task, providers)
    assert result["error_code"] == "tool_not_authorized"


def test_automatic_rag_uses_real_pipeline_and_preserves_sources(client, storage):
    _, _, first, _ = uploaded(client, "Milvus 保存向量。")
    _, _, second, _ = uploaded(client, "PostgreSQL 保存元数据。")
    _, body, task, _ = setup_task(client, tools=["text_stats"], token_budget=100000)

    def choose(messages):
        rows = json.loads(messages[-1]["content"])["items"]
        assert {item["id"] for item in rows} == {first, second}
        return tool("knowledge_search", knowledge_base_id=first, query="Milvus")

    def search_second(messages):
        sources = json.loads(messages[-1]["content"])["sources"]
        assert sources and "Milvus" in sources[0]["content"]
        return tool("knowledge_search", knowledge_base_id=second, query="PostgreSQL")

    result = execute(
        client,
        task,
        [
            Script([tool("delegate_task", task="比较用户资料"), "综合结论"], ["delegate_task"]),
            Script(
                [
                    tool("list_knowledge_bases"),
                    choose,
                    search_second,
                    tool("text_stats", text="结果"),
                    "带依据的摘要",
                ],
                WORKER_TOOLS,
            ),
        ],
    )
    assert result["status"] == "succeeded", result
    messages = client.get(f"/api/v1/conversations/{body['conversation_id']}/messages").json()
    sources = messages["items"][-1]["sources"]
    assert {item["knowledge_base_id"] for item in sources} == {first, second}
    for source in sources:
        response = client.get(
            f"/api/v1/sources/{source['chunk_id']}", params={"evidence_id": source["evidence_id"]}
        )
        assert response.status_code == 200


def test_other_users_knowledge_is_not_listed_or_searched(client, storage, monkeypatch):
    _, _, foreign, _ = uploaded(client)
    foreign_user = login(client)["user"]["id"]
    # 将知识库移给另一个真实用户；发起任务仍为原用户。
    from app.identity.models import User
    from app.rag.models import KnowledgeBase

    with Session(client.app.state.database) as db:
        other = db.scalar(select(User).where(User.id != foreign_user))
        db.get(KnowledgeBase, foreign).account_id = other.id
        db.commit()
    _, _, task, _ = setup_task(client, tools=["text_stats"], token_budget=100000)
    monkeypatch.setattr(
        "app.agents.subagents.RagPipeline.run", lambda *args: pytest.fail("跨用户请求不能进入检索")
    )

    def attempt(messages):
        assert json.loads(messages[-1]["content"])["items"] == []
        return tool("knowledge_search", knowledge_base_id=foreign, query="Milvus")

    def unavailable(messages):
        assert json.loads(messages[-1]["content"])["error_code"] == "knowledge_unavailable"
        return "无权访问"

    result = execute(
        client,
        task,
        [
            Script([tool("delegate_task", task="查资料"), "没有资料"], ["delegate_task"]),
            Script([tool("list_knowledge_bases"), attempt, unavailable], WORKER_TOOLS),
        ],
    )
    assert result["status"] == "succeeded", result


@pytest.mark.parametrize("completed", [False, True])
def test_resume_keeps_parent_and_child_checkpoints_and_reuses_tools(client, completed):
    _, _, task, _ = setup_task(client, tools=["text_stats"], token_budget=100000)

    class Crash(BaseException):
        pass

    def crash(messages):
        raise Crash()

    router = Script([tool("delegate_task", task="统计"), crash], ["delegate_task"])
    worker = Script(
        [tool("text_stats", text="abc"), "子结果" if completed else crash], WORKER_TOOLS
    )
    with pytest.raises(Crash):
        execute(client, task, [router, worker])
    with Session(client.app.state.database) as db:
        checkpoint = db.get(Run, task["id"]).checkpoint
        assert checkpoint["node"] == ("model" if completed else "tools")
        assert len(checkpoint["subagents"]) == 1
        assert len(list(db.scalars(select(ToolExecution)))) == 1
    expire_lease(client, task["id"])
    recover(client)
    providers = [Script(["最终答案"], ["delegate_task"])]
    if not completed:
        providers.append(Script(["恢复后的子结果"], WORKER_TOOLS))
    result = execute(client, task, providers)
    assert result["status"] == "succeeded", result
    with Session(client.app.state.database) as db:
        assert len(list(db.scalars(select(ToolExecution)))) == 1


def test_cancel_inside_subagent_stops_before_business_tool(client):
    _, _, task, _ = setup_task(client, tools=["text_stats"], token_budget=100000)

    def cancel(messages):
        with Session(client.app.state.database) as db:
            db.get(Run, task["id"]).cancel_requested = True
            db.commit()
        return tool("text_stats", text="不能执行")

    result = execute(
        client,
        task,
        [
            Script([tool("delegate_task", task="统计")], ["delegate_task"]),
            Script([cancel], WORKER_TOOLS),
        ],
    )
    assert result["status"] == "cancelled"
    with Session(client.app.state.database) as db:
        assert db.scalar(select(ToolExecution)) is None


def test_subagent_spends_parent_budget(client):
    _, _, task, _ = setup_task(client, tools=["text_stats"], token_budget=10000)
    result = execute(
        client,
        task,
        [
            Script([tool("delegate_task", task="统计")], ["delegate_task"]),
            Script([tool("text_stats", text="abc"), "子结果"], WORKER_TOOLS),
        ],
    )
    assert result["error_code"] == "token_budget_exhausted"
    assert result["charged_tokens"] <= result["reserved_tokens"]


def test_completed_delegate_is_reused_before_parent_checkpoint_commit(client, monkeypatch):
    _, _, task, _ = setup_task(client, tools=["text_stats"], token_budget=100000)
    original = Execution.save

    class Crash(BaseException):
        pass

    def save(execution, state, node):
        if node == "model" and state["tool_count"] == 1:
            raise Crash()
        original(execution, state, node)

    with monkeypatch.context() as patch:
        patch.setattr(Execution, "save", save)
        with pytest.raises(Crash):
            execute(
                client,
                task,
                [
                    Script([tool("delegate_task", task="统计")], ["delegate_task"]),
                    Script([tool("text_stats", text="abc"), "子结果"], WORKER_TOOLS),
                ],
            )
    with Session(client.app.state.database) as db:
        checkpoint = db.get(Run, task["id"]).checkpoint
        assert checkpoint["node"] == "tools"
        assert next(iter(checkpoint["subagents"].values()))["result"]["summary"] == "子结果"
    expire_lease(client, task["id"])
    recover(client)
    result = execute(client, task, [Script(["最终答案"], ["delegate_task"])])
    assert result["status"] == "succeeded", result


def test_deleted_source_cannot_be_used_after_child_finishes(client, storage):
    _, _, base_id, document = uploaded(client)
    _, _, task, _ = setup_task(client, tools=["text_stats"], token_budget=100000)

    def remove_source(messages):
        from app.rag.models import Document

        with Session(client.app.state.database) as db:
            db.get(Document, document["id"]).status = "deleted"
            db.commit()
        return "不得公开的过期答案"

    result = execute(
        client,
        task,
        [
            Script([tool("delegate_task", task="查资料"), remove_source], ["delegate_task"]),
            Script(
                [tool("knowledge_search", knowledge_base_id=base_id, query="Milvus"), "子结果"],
                WORKER_TOOLS,
            ),
        ],
    )
    assert result["error_code"] == "knowledge_source_removed"
    assert "不得公开的过期答案" not in client.get(task["events_url"]).text


def test_mock_automatically_searches_empty_knowledge_without_selector(client):
    from app.rag.models import KnowledgeBase

    _, body, task, _ = setup_task(
        client, "根据我的知识库说明项目部署方法", tools=[], token_budget=100000,
    )
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        assert "knowledge_base_id" not in run.snapshot
        db.add(KnowledgeBase(account_id=run.owner_id, name="项目资料"))
        db.commit()
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute()
    result = client.get(f"/api/v1/runs/{task['id']}").json()
    assert result["status"] == "succeeded", result
    messages = client.get(f"/api/v1/conversations/{body['conversation_id']}/messages").json()
    assert "没有找到充分的知识库证据" in messages["items"][-1]["content"]
    assert messages["items"][-1]["sources"] == []
    events = client.get(task["events_url"]).text
    assert "list_knowledge_bases" in events and "knowledge_search" in events

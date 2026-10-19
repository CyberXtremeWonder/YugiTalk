"""工具下线兼容、检查点、租约栅栏与预算增量结算；宕机用未捕获退出模拟。"""

from datetime import timedelta
from uuid import uuid4

import pytest
from app.agents.models import AgentVersion
from app.chat.models import Message
from app.core.entities import now
from app.providers.client import ProviderFailure, ToolCall
from app.rag.indexing import index_document
from app.rag.models import Document
from app.tasks.execution import Execution, finish_run
from app.tasks.ledger_models import Approval, Ticket, ToolExecution
from app.tasks.models import Budget, Run, RunEvent
from app.tasks.reliability import execute_bound_tool, fingerprint, sweep
from app.identity.models import User
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_agent_graph import ScriptedProvider
from test_agents import setup_task
from test_knowledge import base

pytest_plugins = ["test_identity", "test_knowledge"]


def expire_lease(client, key):
    """使任务租约过期，模拟进程中断后的恢复条件。"""
    with Session(client.app.state.database) as db:
        run = db.get(Run, key)
        run.lease_until = now() - timedelta(seconds=1)
        db.commit()


def recover(client):
    """执行隔离恢复扫描，验证任务补投与终态处理。"""
    dispatched = []
    result = sweep(
        client.app.state.database, client.app.state.settings, dispatched.append, lambda *args: None
    )
    return result, dispatched


def test_removed_ticket_tool_and_routes_are_unavailable(client):
    """验证已移除的工单工具与业务接口不可调用。"""
    headers, body, run, agent = setup_task(client)
    with Session(client.app.state.database) as db:
        space = db.get(Run, run["id"]).account_id
    for path, data in (
        (f"/me/agents", {"name": "旧写工具", "tools": ["create_ticket"]}),
        (f"/agents/{agent['id']}/versions", {"name": "旧写工具", "tools": ["create_ticket"]}),
    ):
        assert client.post("/api/v1" + path, json=data, headers=headers).status_code == 422
    assert client.get("/api/v1/model/capabilities").json()["tools"] == [
        "text_stats",
        "format_report",
        "traffic_query",
        "weather_query",
        "web_search",
    ]
    assert client.get(f"/api/v1/me/tickets").status_code == 404
    assert client.get(f"/api/v1/runs/{run['id']}/approvals").status_code == 404
    assert (
        client.post(
            f"/api/v1/approvals/{uuid4()}/decision",
            headers=headers,
            json={"decision": "approve", "argument_hash": "a" * 64},
        ).status_code
        == 404
    )


def test_legacy_agent_tools_are_filtered_without_mutating_published_version(client):
    """验证运行时过滤下线工具，历史发布版本保持原始内容。"""
    headers, body, run, agent = setup_task(client, tools=["format_report"], token_budget=30000)
    with Session(client.app.state.database) as db:
        version = db.get(AgentVersion, agent["version_id"])
        version.config = {**version.config, "tools": ["format_report", "create_ticket"]}
        space = db.get(Run, run["id"]).account_id
        db.commit()
    assert client.get(f"/api/v1/me/agents").json()["items"][0]["config"][
        "tools"
    ] == ["format_report"]
    client.post(f"/api/v1/runs/{run['id']}/cancel", headers=headers)
    headers["Idempotency-Key"] = str(uuid4())
    task = client.post(
        "/api/v1/runs", json={**body, "message": "请生成报告"}, headers=headers
    ).json()
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute()
    with Session(client.app.state.database) as db:
        assert db.get(Run, task["id"]).status == "succeeded"
        assert db.get(Run, task["id"]).snapshot["tools"] == ["format_report"]
        assert "create_ticket" in db.get(AgentVersion, agent["version_id"]).config["tools"]


def test_legacy_waiting_task_is_closed_and_historical_ticket_is_preserved(client):
    """验证旧审批等待任务安全结束，并保留历史工单数据。"""
    _, _, run, _ = setup_task(client)
    with Session(client.app.state.database) as db:
        record = db.get(Run, run["id"])
        record.status = "waiting_approval"
        item = ToolExecution(
            run_id=record.id,
            call_key="1:legacy",
            tool="create_ticket",
            arguments={"title": "历史工单", "body": "保留用户数据"},
            argument_hash="a" * 64,
        )
        db.add(item)
        db.flush()
        db.add(
            Approval(
                execution_id=item.id,
                owner_id=record.owner_id,
                argument_hash="a" * 64,
                expires_at=now() + timedelta(days=1),
            )
        )
        db.add(
            Ticket(
                account_id=record.account_id,
                owner_id=record.owner_id,
                execution_id=item.id,
                title="历史工单",
                body="保留用户数据",
            )
        )
        db.commit()
    recover(client)
    with Session(client.app.state.database) as db:
        assert db.get(Run, run["id"]).error_code == "tool_removed"
        assert db.scalar(select(Approval)).status == "cancelled"
        assert db.scalar(select(Ticket)).title == "历史工单"
        assert all(row.reserved == 0 for row in db.scalars(select(Budget)))


def test_removed_tool_cannot_replay_a_cached_execution(client):
    """验证下线工具不能通过旧执行账本重放结果。"""
    _, _, run, _ = setup_task(client)
    call = ToolCall(id="legacy", name="create_ticket", arguments={"title": "a", "body": "b"})
    with Session(client.app.state.database) as db:
        db.add(
            ToolExecution(
                run_id=run["id"],
                call_key="1:legacy",
                tool=call.name,
                arguments=call.arguments,
                argument_hash=fingerprint(call.name, call.arguments),
                status="succeeded",
                result={"ticket_id": "historical"},
            )
        )
        db.commit()
        with pytest.raises(ProviderFailure, match="tool_removed"):
            execute_bound_tool(db, db.get(Run, run["id"]), call, 1)


def test_permission_revocation_during_queue_releases_budget(client):
    """验证排队期间权限撤销会结束任务并释放预算。"""
    _, _, run, _ = setup_task(client)
    with Session(client.app.state.database) as db:
        task = db.get(Run, run["id"])
        db.get(User, task.owner_id).active = False
        db.commit()
    recover(client)
    with Session(client.app.state.database) as db:
        assert db.get(Run, run["id"]).error_code == "permission_revoked"
        assert all(row.reserved == 0 for row in db.scalars(select(Budget)))


def test_crash_after_tool_result_before_checkpoint_reuses_ledger(client, monkeypatch):
    """验证工具结果提交后中断可复用账本，避免工具重复执行。"""
    _, _, run, _ = setup_task(client, "请生成报告", tools=["format_report"], token_budget=30000)
    original = Execution.save

    def crash(self, state, node):
        """在指定提交边界模拟进程中断，验证检查点恢复。"""
        if state["tool_cursor"] == 1:
            raise SystemExit("模拟工具结果提交后 Worker 宕机")
        original(self, state, node)

    monkeypatch.setattr(Execution, "save", crash)
    with pytest.raises(SystemExit):
        Execution(client.app.state.database, client.app.state.settings, run["id"]).execute()
    with Session(client.app.state.database) as db:
        assert db.scalar(select(ToolExecution)).status == "succeeded"
    expire_lease(client, run["id"])
    assert run["id"] in recover(client)[1]
    monkeypatch.setattr(Execution, "save", original)
    monkeypatch.setattr(
        "app.tasks.reliability.execute_tool",
        lambda *args: (_ for _ in ()).throw(AssertionError("工具不应再次执行")),
    )
    Execution(client.app.state.database, client.app.state.settings, run["id"]).execute()
    with Session(client.app.state.database) as db:
        task = db.get(Run, run["id"])
        assert task.status == "succeeded"
        assert db.scalar(select(func.count()).select_from(ToolExecution)) == 1
        assert (
            db.scalar(select(func.count()).select_from(Message).where(Message.role == "assistant"))
            == 1
        )
        assert all(
            row.reserved == 0 and row.spent == task.charged_tokens
            for row in db.scalars(select(Budget))
        )


def test_model_crash_restores_checkpoint_and_fences_stale_worker(client):
    """验证模型中断后从检查点恢复，旧工作进程不能覆盖新状态。"""
    _, _, run, _ = setup_task(client, token_budget=30000)

    class Crashing(ScriptedProvider):
        """在输出期间抛出受控故障，模拟中断的模型调用。"""

        def stream(self, *args):
            """逐步产生公开文本和结构化回复，并在调用期间检查取消与执行边界。"""
            raise SystemExit("模拟模型请求中宕机")
            yield

    old = Execution(client.app.state.database, client.app.state.settings, run["id"])
    with pytest.raises(SystemExit):
        old.execute(lambda *args: Crashing([]))
    assert recover(client)[1] == []
    expire_lease(client, run["id"])
    assert run["id"] in recover(client)[1]
    with pytest.raises(ProviderFailure):
        old.update("text_delta", {"text": "过期输出"})
    finish_run(
        client.app.state.database,
        run["id"],
        "succeeded",
        None,
        "过期结果",
        0,
        None,
        None,
        old.token,
    )
    Execution(client.app.state.database, client.app.state.settings, run["id"]).execute()
    state = client.get(f"/api/v1/runs/{run['id']}").json()
    assert state["status"] == "succeeded" and state["attempts"] == 2
    events = client.get(run["events_url"]).text
    assert "checkpoint_restored" in events and "过期输出" not in events and "过期结果" not in events


def test_retry_new_key_and_incremental_budget(client):
    """验证新的重试幂等键与增量预算结算规则。"""
    headers, _, run, _ = setup_task(client, token_budget=30000)

    class Failed(ScriptedProvider):
        """模拟可重试模型失败，验证重试状态与预算结算。"""

        def stream(self, *args):
            """逐步产生公开文本和结构化回复，并在调用期间检查取消与执行边界。"""
            raise ProviderFailure("model_timeout")
            yield

    Execution(client.app.state.database, client.app.state.settings, run["id"]).execute(
        lambda *args: Failed([])
    )
    assert client.get(f"/api/v1/runs/{run['id']}").json()["retryable"]
    headers["Idempotency-Key"] = str(uuid4())
    path = f"/api/v1/runs/{run['id']}/retry"
    assert client.post(path, headers=headers).status_code == 202
    assert client.post(path, headers=headers).status_code == 202
    Execution(client.app.state.database, client.app.state.settings, run["id"]).execute()
    assert client.post(path, headers=headers).status_code == 202
    headers["Idempotency-Key"] = str(uuid4())
    assert client.post(path, headers=headers).status_code == 409
    with Session(client.app.state.database) as db:
        task = db.get(Run, run["id"])
        assert all(
            row.reserved == 0 and row.spent == task.charged_tokens
            for row in db.scalars(select(Budget))
        )


def test_queued_intent_survives_broker_error_and_recovery_cap(client):
    """验证队列故障不丢失持久排队意图，并限制恢复次数。"""
    headers, body, run, _ = setup_task(client)
    client.post(f"/api/v1/runs/{run['id']}/cancel", headers=headers)

    def unavailable(*args):
        """模拟队列不可用，验证已提交排队意图不会丢失。"""
        raise RuntimeError("Redis 暂不可用")

    client.app.state.dispatch_run = unavailable
    headers["Idempotency-Key"] = str(uuid4())
    task = client.post("/api/v1/runs", json=body, headers=headers)
    assert task.status_code == 202 and task.json()["status"] == "queued"
    key = task.json()["id"]
    assert key in recover(client)[1]
    with Session(client.app.state.database) as db:
        record = db.get(Run, key)
        record.status = "running"
        record.attempts = client.app.state.settings.run_max_attempts
        record.lease_until = now() - timedelta(seconds=1)
        db.commit()
    recover(client)
    assert client.get(f"/api/v1/runs/{key}").json()["error_code"] == "worker_lost"


def test_document_crash_new_generation_and_stale_index_fence(client, storage):
    """验证资料恢复生成新索引代次，并隔离旧索引结果。"""
    _, headers, key = base(client)
    doc = client.post(
        f"/api/v1/knowledge-bases/{key}/documents?filename=租约.md",
        content="资料使用 Milvus。".encode(),
        headers=headers,
    ).json()
    with Session(client.app.state.database) as db:
        item = db.get(Document, doc["id"])
        item.status, item.lease_token, item.lease_until = (
            "indexing",
            "old",
            now() - timedelta(seconds=1),
        )
        item.attempts = 1
        db.commit()
    sweep(
        client.app.state.database, client.app.state.settings, lambda *args: None, lambda *args: None
    )
    index_document(client.app.state.database, client.app.state.settings, doc["id"], 1)
    with Session(client.app.state.database) as db:
        assert db.get(Document, doc["id"]).status == "queued"
        assert db.get(Document, doc["id"]).index_version == 2
    index_document(client.app.state.database, client.app.state.settings, doc["id"], 2)
    with Session(client.app.state.database) as db:
        assert db.get(Document, doc["id"]).status == "ready"


def test_document_external_write_crosses_lease_does_not_publish_sql(client, storage, monkeypatch):
    """验证外部写入跨越租约有效期后不能发布资料数据库状态。"""
    from app.rag.models import Chunk

    _, headers, key = base(client)
    doc = client.post(
        f"/api/v1/knowledge-bases/{key}/documents?filename=超时.md",
        content="租约到期不发布半成品。".encode(),
        headers=headers,
    ).json()

    class LateIndex:
        """模拟租约过期后才完成写入的旧索引进程。"""

        def __init__(self, settings):
            """保存实例所需的依赖与配置，初始化后续执行使用的状态。"""
            pass

        def ensure(self, embedding):
            """检查或创建索引集合，并验证现有向量字段维度。"""
            pass

        def upsert(self, collection, rows):
            """向指定集合批量写入或更新带版本标识的向量条目。"""
            deadline = now() + timedelta(seconds=client.app.state.settings.run_lease_seconds + 1)
            monkeypatch.setattr("app.rag.indexing.now", lambda: deadline)

    monkeypatch.setattr("app.rag.indexing.VectorIndex", LateIndex)
    index_document(client.app.state.database, client.app.state.settings, doc["id"], 1)
    with Session(client.app.state.database) as db:
        assert db.get(Document, doc["id"]).status == "indexing"
        assert not db.scalars(select(Chunk).where(Chunk.document_id == doc["id"])).all()


def test_completed_graph_checkpoint_recovery_skips_model(client, monkeypatch):
    """验证完成节点的检查点恢复不会重新调用模型。"""
    _, _, run, _ = setup_task(client, token_budget=30000)
    original = finish_run
    monkeypatch.setattr(
        "app.tasks.execution.finish_run",
        lambda *args: (_ for _ in ()).throw(SystemExit("图完成后宕机")),
    )
    with pytest.raises(SystemExit):
        Execution(client.app.state.database, client.app.state.settings, run["id"]).execute()
    monkeypatch.setattr("app.tasks.execution.finish_run", original)
    expire_lease(client, run["id"])
    recover(client)
    provider = ScriptedProvider([])
    Execution(client.app.state.database, client.app.state.settings, run["id"]).execute(
        lambda *args: provider
    )
    assert provider.calls == 0
    assert client.get(f"/api/v1/runs/{run['id']}").json()["status"] == "succeeded"


def test_late_retrieval_cannot_overwrite_new_lease_snapshot(client, monkeypatch):
    """验证迟到的检索结果不能覆盖新租约下的任务快照。"""
    from sqlalchemy import update

    _, _, key, _ = setup_task(client, token_budget=30000)
    with Session(client.app.state.database) as db:
        run = db.get(Run, key["id"])
        run.snapshot = {**run.snapshot, "knowledge_base_id": str(uuid4())}
        db.commit()

    def takeover(self, *args):
        # 模拟检索返回前租约已被回收，旧 Worker 收到晚到的外部结果。
        """模拟新进程取得租约，验证迟到结果无法覆盖新状态。"""
        with Session(client.app.state.database) as db:
            db.execute(update(Run).where(Run.id == key["id"]).values(lease_token="new-worker"))
            db.commit()
        return {"phase": "done", "sources": [{"label": "S1", "content": "旧结果不应覆盖新检查点"}]}

    monkeypatch.setattr("app.tasks.execution.RagPipeline.run", takeover)
    Execution(client.app.state.database, client.app.state.settings, key["id"]).execute()
    with Session(client.app.state.database) as db:
        run = db.get(Run, key["id"])
        assert run.status == "running" and run.lease_token == "new-worker"
        assert "sources" not in run.snapshot
        assert not any(
            row.payload.get("stage") == "retrieval"
            for row in db.scalars(select(RunEvent).where(RunEvent.run_id == run.id))
        )

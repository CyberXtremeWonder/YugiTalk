"""任务保留上限、倒序分页、关联清理和聊天附件独立保留回归。"""

import base64
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_agents import setup_task
from test_identity import login

from app.chat.models import Conversation, Message
from app.core.entities import now
from app.identity.models import User
from app.observability.trace_models import TraceStep
from app.research.models import ResearchProject, ResearchReport
from app.tasks.ledger_models import Approval, RunRetry, Ticket, ToolExecution
from app.tasks.models import Budget, Run, RunEvent
from app.tasks.retention import prune_history, prune_user_history

pytest_plugins = ["test_identity"]


def seed(client, count=105):
    """建立有确定时间顺序的本人任务，不执行模型或网络查询。"""
    headers, body, task, _ = setup_task(client, "第一条任务", tools=["text_stats"])
    with Session(client.app.state.database) as db:
        first = db.get(Run, task["id"])
        owner = first.owner_id
        started = now() - timedelta(days=1)
        first.status, first.created_at, first.finished_at = "succeeded", started, started
        conversation = db.get(Conversation, first.conversation_id)
        conversation.title = "企业资料讨论"
        ids = [first.id]
        for number in range(1, count):
            row = Run(
                account_id=owner,
                owner_id=owner,
                conversation_id=first.conversation_id,
                agent_version_id=first.agent_version_id,
                snapshot=first.snapshot,
                idempotency_key=f"seed-{number}",
                request_hash="seed",
                reserved_tokens=0,
                prompt=f"第{number}次提问\n" + "具体资料 " * 60,
                status="succeeded",
                created_at=started + timedelta(seconds=number),
                finished_at=started,
            )
            db.add(row)
            db.flush()
            ids.append(row.id)
        db.commit()
    return owner, ids, headers, body


def test_newest_hundred_have_title_preview_and_stable_pagination(client):
    _, ids, _, _ = seed(client)
    found = []
    path = "/api/v1/me/runs?limit=17"
    while path:
        response = client.get(path)
        assert response.status_code == 200, response.text
        data = response.json()
        for row in data["items"]:
            assert row["conversation_title"] == "企业资料讨论"
            assert len(row["prompt_preview"]) <= 160 and "\n" not in row["prompt_preview"]
            assert row["created_at"]
            found.append(row["id"])
        path = (
            f"/api/v1/me/runs?limit=17&cursor={data['next_cursor']}" if data["next_cursor"] else ""
        )
    assert found == list(reversed(ids[-100:]))


def test_retention_cleans_execution_data_preserving_messages_files_and_budgets(client):
    owner, ids, _, body = seed(client)
    engine = client.app.state.database
    with Session(engine) as db:
        execution = ToolExecution(
            run_id=ids[0],
            call_key="one",
            tool="text_stats",
            arguments={},
            argument_hash="hash",
            status="succeeded",
            result={},
        )
        db.add(execution)
        db.flush()
        execution_id = execution.id
        ticket = Ticket(
            account_id=owner,
            owner_id=owner,
            execution_id=execution_id,
            title="历史工单",
            body="保留内容",
        )
        db.add_all(
            [
                ticket,
                Approval(
                    execution_id=execution_id,
                    owner_id=owner,
                    status="approved",
                    argument_hash="hash",
                    expires_at=now(),
                ),
                RunRetry(run_id=ids[0], owner_id=owner, key="retry"),
                RunEvent(run_id=ids[0], sequence=1, kind="completed", payload={}),
                TraceStep(run_id=ids[0], attempt=1, kind="llm", name="model", status="succeeded"),
                Message(
                    id=ids[0],
                    conversation_id=body["conversation_id"],
                    sequence=2,
                    role="assistant",
                    content="原对话仍然保留",
                ),
            ]
        )
        project = ResearchProject(
            owner_id=owner, title="报告", question="问题", options={}, state={"run_id": ids[0]}
        )
        db.add(project)
        db.flush()
        project_id = project.id
        db.add(
            ResearchReport(
                project_id=project_id,
                run_id=ids[0],
                version=1,
                markdown="报告",
                data={
                    "files": {
                        "md": {
                            "filename": "报告.md",
                            "size": 6,
                            "media_type": "text/markdown",
                            "content": base64.b64encode("报告".encode()).decode(),
                        }
                    }
                },
            )
        )
        db.commit()
        ticket_id = ticket.id
        budgets = [(r.key, r.reserved, r.spent) for r in db.scalars(select(Budget))]
        assert prune_user_history(db, owner) == 5
        db.commit()
    with Session(engine) as db:
        assert db.get(Run, ids[0]) is None
        assert db.get(ToolExecution, execution_id) is None
        assert db.get(Ticket, ticket_id).execution_id is None
        assert db.scalar(select(func.count()).select_from(Run)) == 100
        for model in (Approval, RunRetry, RunEvent, TraceStep):
            assert db.scalar(select(func.count()).select_from(model)) == 0
        assert [(r.key, r.reserved, r.spent) for r in db.scalars(select(Budget))] == budgets
    history = client.get(f"/api/v1/conversations/{body['conversation_id']}/messages").json()
    assert history["items"][-1]["content"] == "原对话仍然保留"
    assert history["items"][-1]["attachments"][0]["available"]
    assert client.get(f"/api/v1/research/{project_id}/files/md").content.decode() == "报告"


def test_active_old_runs_are_preserved_until_terminal(client):
    owner, ids, _, _ = seed(client)
    with Session(client.app.state.database) as db:
        db.get(Run, ids[0]).status = "running"
        db.get(Run, ids[1]).status = "waiting_input"
        db.commit()
        assert prune_user_history(db, owner) == 3
        db.commit()
        assert db.get(Run, ids[0]) and db.get(Run, ids[1])
        db.get(Run, ids[0]).status = "succeeded"
        db.get(Run, ids[1]).status = "cancelled"
        db.commit()
    assert prune_history(client.app.state.database) == 2


def test_same_timestamp_pagination_is_deterministic(client):
    _, ids, _, _ = seed(client, 8)
    with Session(client.app.state.database) as db:
        timestamp = now()
        for row in db.scalars(select(Run)):
            row.created_at = timestamp
        db.commit()
    first = client.get("/api/v1/me/runs?limit=3").json()
    second = client.get(f"/api/v1/me/runs?limit=5&cursor={first['next_cursor']}").json()
    assert [r["id"] for r in first["items"] + second["items"]] == sorted(ids, reverse=True)
    assert second["next_cursor"] is None


def test_trace_finalization_is_not_removed_mid_write(client):
    owner, ids, _, _ = seed(client)
    with Session(client.app.state.database) as db:
        db.get(Run, ids[0]).finished_at = now()
        root = TraceStep(
            run_id=ids[0], attempt=1, kind="execution", name="closing", status="running"
        )
        db.add(root)
        db.commit()
        assert prune_user_history(db, owner) == 4
        db.commit()
        assert db.get(Run, ids[0]) is not None
        root.status = "succeeded"
        db.commit()
        assert prune_user_history(db, owner) == 1
        db.commit()


def test_creation_prunes_old_history_and_other_user_cannot_read_preview(client):
    _, _, headers, body = seed(client)
    headers["Idempotency-Key"] = str(uuid4())
    response = client.post("/api/v1/runs", headers=headers, json={**body, "message": "新的提问"})
    assert response.status_code == 202, response.text
    assert (
        client.get("/api/v1/me/runs?limit=100").json()["items"][0]["prompt_preview"] == "新的提问"
    )
    with Session(client.app.state.database) as db:
        assert db.scalar(select(func.count()).select_from(Run)) == 100
    login(client, "other@example.test")
    assert client.get("/api/v1/me/runs").json()["items"] == []
    with Session(client.app.state.database) as db:
        user = db.scalar(select(User).where(User.email == "other@example.test"))
        user.platform_admin = True
        db.commit()
    traces = client.get("/api/v1/admin/traces").json()["items"]
    assert traces and all(row["prompt_preview"] is None for row in traces)

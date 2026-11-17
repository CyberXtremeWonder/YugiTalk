"""并行对话、定向补充和模型阶段边界；不访问真实模型或搜索服务。"""

import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session
from test_agents import setup_task

from app.chat.models import Message
from app.memory.context import build_messages
from app.memory.models import ConversationSummary
from app.providers.client import ModelEvent, Reply
from app.tasks.execution import Execution
from app.tasks.models import Run

pytest_plugins = ["test_identity"]


def post(client, headers, body, message, **extra):
    return client.post(
        "/api/v1/runs",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={**body, "message": message, **extra},
    )


def test_parallel_tasks_keep_context_and_reply_ownership(client):
    client.app.state.settings.cumulative_budget_enabled = False
    headers, body, first, _ = setup_task(client, "研究甲", token_budget=100000)
    response = post(client, headers, body, "解释乙")
    assert response.status_code == 202, response.text
    second = response.json()
    assert second["id"] != first["id"] and second["status"] == "queued"
    client.app.state.settings.task_budget_enabled = False
    seen = []

    class Provider:
        def stream(self, messages, tools, max_tokens):
            seen.append(json.dumps(messages, ensure_ascii=False))
            yield ModelEvent("text", text="乙的回答" if "解释乙" in seen[-1] else "甲的回答")
            yield ModelEvent("reply", reply=Reply())

    for task in (second, first):
        Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(
            lambda *args: Provider()
        )
        assert client.get(f"/api/v1/runs/{task['id']}").json()["status"] == "succeeded"
    assert "解释乙" not in seen[-1] and "乙的回答" not in seen[-1]
    assert "parallel_task" in seen[0]
    rows = client.get(f"/api/v1/conversations/{body['conversation_id']}/messages").json()["items"]
    assert next(r for r in rows if r["id"] == first["id"])["content"] == "甲的回答"
    assert next(r for r in rows if r["id"] == second["id"])["content"] == "乙的回答"
    assert rows[0]["run_id"] == first["id"] and rows[1]["run_id"] == second["id"]


def test_future_summary_does_not_leak_or_regress(client):
    _, _, task, _ = setup_task(client)
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        for seq in range(2, 14):
            db.add(
                Message(
                    conversation_id=run.conversation_id,
                    sequence=seq,
                    role="user",
                    content=f"历史{seq}",
                )
            )
        db.add(
            ConversationSummary(
                conversation_id=run.conversation_id,
                upto_sequence=14,
                content="FUTURE_PRIVATE",
                source_ids=[],
            )
        )
        run.snapshot = {**run.snapshot, "conversation_upto_sequence": 13}
        db.commit()
        messages = build_messages(db, run, lambda payload: "当前截止序号之前的模型摘要")
        assert "FUTURE_PRIVATE" not in json.dumps(messages)
        db.commit()
        assert db.get(ConversationSummary, run.conversation_id).upto_sequence == 14


def test_modification_messages_create_independent_tasks(client):
    client.app.state.settings.cumulative_budget_enabled = False
    headers, body, first, _ = setup_task(client, "研究并生成报告")
    response = post(client, headers, body, "报告改成800字")
    assert response.status_code == 202, response.text
    assert response.json()["id"] != first["id"]
    with Session(client.app.state.database) as db:
        assert "supplements" not in db.get(Run, first["id"]).checkpoint
        assert len(list(db.scalars(select(Run)))) == 2
    assert post(client, headers, body, "第三个独立问题").status_code == 429
    response = post(client, headers, body, "改成900字", followup_run_id=first["id"])
    assert response.status_code == 422

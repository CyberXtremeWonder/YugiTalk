"""凭证隔离、附件生命周期、活动任务入口及跨版本幂等回归。"""

import asyncio
import hashlib
import json
import time
from datetime import timedelta
from importlib import import_module
from uuid import uuid4

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from test_agents import setup_task
from test_identity import ORIGIN, login
from test_rag_model_connection import response_frame

from app.agents.model_config import model_settings
from app.agents.models import AgentVersion
from app.chat.models import Message
from app.core.entities import now
from app.memory.context import build_messages
from app.rag.contracts import EvidenceGrade, QueryPlan
from app.rag.model import StructuredModel
from app.tasks.contracts import RunInput
from app.tasks.models import Run
from app.tasks.retention import prune_user_history

pytest_plugins = ["test_identity"]
MARKER = "UNIQUE_INVOICE_TOTAL_73921"


def prepare(client):
    """建立隔离对话并结束初始任务，释放并发名额。"""
    client.app.state.settings.cumulative_budget_enabled = False
    client.app.state.settings.task_budget_enabled = False
    headers, body, task, _ = setup_task(client, "initial message")
    assert client.post(f"/api/v1/runs/{task['id']}/cancel", headers=headers).status_code == 200
    return headers, body


def attachment_run(client):
    """通过真实上传及任务接口保存文本附件。"""
    headers, body = prepare(client)
    parsed = client.post(
        "/api/v1/chat/uploads",
        content=MARKER.encode(),
        headers={
            **headers,
            "X-Filename": "invoice.txt",
            "Content-Type": "application/octet-stream",
        },
    )
    assert parsed.status_code == 200, parsed.text
    body = {**body, "message": "Analyze the attachment", "uploaded_files": [parsed.json()]}
    headers = {**headers, "Idempotency-Key": str(uuid4())}
    created = client.post("/api/v1/runs", json=body, headers=headers)
    assert created.status_code == 202, created.text
    return headers, body, created.json()


def seed_newer_runs(db, first, count=100):
    """生成超过历史窗口的终态记录，不触发真实模型或队列。"""
    first.created_at = now() - timedelta(days=2)
    for i in range(count):
        db.add(
            Run(
                account_id=first.account_id,
                owner_id=first.owner_id,
                conversation_id=first.conversation_id,
                agent_version_id=first.agent_version_id,
                snapshot={
                    **first.snapshot,
                    "uploaded_files": [],
                    "conversation_upto_sequence": i + 100,
                },
                idempotency_key=f"seed-{i}",
                request_hash="fixture",
                prompt="later task",
                status="succeeded",
                reserved_tokens=0,
                created_at=now() + timedelta(seconds=i),
                finished_at=now(),
            )
        )


@pytest.mark.parametrize("purpose,role", [("plan", "fast"), ("grade", "grader")])
def test_server_role_key_without_model_stays_on_server_connection(
    client, monkeypatch, purpose, role
):
    """普通用户自定义地址不得收到部署密钥；空角色模型沿用服务端默认模型。"""
    server = client.app.state.settings
    for key, value in {
        "model_provider": "openai_compatible",
        "model_id": "server-main",
        "model_base_url": "https://server.fixture/v1",
        "model_api_key": SecretStr("server-key"),
        "rag_fast_model": "",
        "rag_grader_model": "",
        f"rag_{role}_api_key": SecretStr("deployment-role-key-fixture"),
    }.items():
        monkeypatch.setattr(server, key, value)
    identity = login(client, "member@example.test")
    assert identity["user"]["platform_admin"] is False
    created = client.post(
        "/api/v1/me/agents",
        headers={
            **ORIGIN,
            "X-CSRF-Token": identity["csrf_token"],
        },
        json={
            "name": "member-owned endpoint",
            "provider": "openai_compatible",
            "model": "agent-model",
            "base_url": "https://user-controlled.fixture/v1",
            "api_key": "agent-key-fixture",
        },
    )
    assert created.status_code == 201, created.text
    with Session(client.app.state.database) as db:
        config = db.get(AgentVersion, created.json()["version_id"]).config
        agent = model_settings(server, config)
    assert agent.model_api_key.get_secret_value() == "agent-key-fixture"
    requests = []
    payload = (
        {"complexity": "simple", "sub_questions": []}
        if purpose == "plan"
        else {
            "relevance": "strong",
            "answerability": "sufficient",
            "ambiguity": "none",
            "route": "answer",
        }
    )

    def respond(request):
        """仅模拟网络传输，验证真实连接组装和供应商协议。"""
        requests.append(request)
        return httpx.Response(200, text=response_frame("openai_compatible", payload))

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.rag.model.httpx.AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(respond),
            **kwargs,
        ),
    )
    asyncio.run(
        StructuredModel(agent, lambda *args: None, role_settings=server).call(
            QueryPlan if purpose == "plan" else EvidenceGrade,
            purpose,
            {},
            time.monotonic() + 3,
        )
    )
    assert len(requests) == 1
    assert str(requests[0].url) == "https://server.fixture/v1/chat/completions"
    assert requests[0].headers["Authorization"] == "Bearer deployment-role-key-fixture"
    assert json.loads(requests[0].content)["model"] == "server-main"


def test_attachment_enters_summary_and_survives_task_retention(client):
    """旧任务被清理后，消息附件仍用于历史展示及后续摘要。"""
    headers, body, task = attachment_run(client)
    with Session(client.app.state.database) as db:
        assert MARKER in json.dumps(build_messages(db, db.get(Run, task["id"])))
    assert client.post(f"/api/v1/runs/{task['id']}/cancel", headers=headers).status_code == 200
    path = f"/api/v1/conversations/{body['conversation_id']}/messages"
    before = next(
        m for m in client.get(path).json()["items"] if m["sequence"] == task["message_sequence"]
    )
    assert before["uploaded_files"][0]["filename"] == "invoice.txt"
    assert "text" not in before["uploaded_files"][0]
    with Session(client.app.state.database) as db:
        first = db.get(Run, task["id"])
        seed_newer_runs(db, first)
        owner = first.owner_id
        db.commit()
        assert prune_user_history(db, owner) >= 1
        db.commit()
        assert db.get(Run, task["id"]) is None
        for seq in range(3, 15):
            db.add(
                Message(
                    conversation_id=body["conversation_id"],
                    sequence=seq,
                    role="assistant" if seq % 2 else "user",
                    content=f"ordinary turn {seq}",
                )
            )
        db.commit()
    after = next(m for m in client.get(path).json()["items"] if m["id"] == before["id"])
    assert after["uploaded_files"] == before["uploaded_files"]
    followup = client.post(
        "/api/v1/runs",
        json={
            **body,
            "message": "What was the invoice total?",
            "uploaded_files": [],
        },
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert followup.status_code == 202, followup.text
    captured = []

    def summarize(payload):
        """记录模型收到的资料，再模拟提取摘要中的关键事实。"""
        captured.append(payload)
        assert MARKER in json.dumps(payload)
        return MARKER

    with Session(client.app.state.database) as db:
        messages = build_messages(db, db.get(Run, followup.json()["id"]), summarize)
        assert captured and any(
            m["sequence"] == task["message_sequence"] for m in captured[0]["messages"]
        )
        assert MARKER in json.dumps(messages)


@pytest.mark.parametrize(
    "status", ["queued", "running", "paused", "waiting_input", "waiting_approval"]
)
def test_old_active_run_remains_in_both_lists_and_can_be_stopped(client, status):
    """历史窗口之外的活动任务仍可查找和停止，分页不遗漏或重复。"""
    headers, body = prepare(client)
    created = client.post(
        "/api/v1/runs", json=body, headers={**headers, "Idempotency-Key": str(uuid4())}
    )
    assert created.status_code == 202
    task = created.json()
    with Session(client.app.state.database) as db:
        first = db.get(Run, task["id"])
        first.status = status
        seed_newer_runs(db, first)
        db.commit()
    rows = client.get(f"/api/v1/conversations/{body['conversation_id']}/runs").json()["items"]
    assert len(rows) == 101 and task["id"] in {r["id"] for r in rows}
    found = []
    path = "/api/v1/me/runs?limit=17"
    while path:
        response = client.get(path)
        assert response.status_code == 200
        data = response.json()
        found.extend(row["id"] for row in data["items"])
        path = (
            f"/api/v1/me/runs?limit=17&cursor={data['next_cursor']}" if data["next_cursor"] else ""
        )
    assert len(found) == len(set(found)) == 101 and task["id"] in found
    assert (
        client.post(f"/api/v1/runs/{task['id']}/cancel", headers=headers).json()["status"]
        == "cancelled"
    )


@pytest.mark.parametrize("changed", [None, "message", "uploaded_files", "research_project_id"])
def test_legacy_idempotency_accepts_only_identical_requests(client, changed):
    """旧哈希允许原请求重发，但不得用于替换正文、附件或研究目标。"""
    headers, body, task, _ = setup_task(client)
    assert client.post("/api/v1/runs", json=body, headers=headers).json()["id"] == task["id"]
    legacy = RunInput.model_validate(body).model_dump(mode="json")
    legacy.pop("uploaded_files")
    legacy.pop("research_project_id")
    with Session(client.app.state.database) as db:
        first = db.get(Run, task["id"])
        first.request_hash = hashlib.sha256(
            json.dumps(legacy, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        db.commit()
    if changed == "message":
        body = {**body, "message": "different request"}
    elif changed == "uploaded_files":
        body = {**body, "uploaded_files": [{"filename": "x.txt", "text": "different", "size": 9}]}
    elif changed == "research_project_id":
        body = {**body, "research_project_id": str(uuid4())}
    response = client.post("/api/v1/runs", json=body, headers=headers)
    assert response.status_code == (409 if changed else 202)
    if not changed:
        assert response.json()["id"] == task["id"]


def test_message_upload_migration_backfills_only_matching_user_messages():
    """在线迁移回填正确消息，保留空消息，失效的旧摘要等待重建。"""
    migration = import_module("migrations.versions.0026_message_uploads")
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE messages (conversation_id TEXT, sequence INTEGER, role TEXT)"
        )
        connection.exec_driver_sql("CREATE TABLE runs (conversation_id TEXT, snapshot JSON)")
        connection.exec_driver_sql(
            "CREATE TABLE conversation_summaries (conversation_id TEXT, upto_sequence INTEGER)"
        )
        connection.exec_driver_sql(
            "INSERT INTO messages VALUES ('a', 1, 'user'), ('a', 2, 'assistant'), ('b', 1, 'user')"
        )
        connection.exec_driver_sql("INSERT INTO conversation_summaries VALUES ('a', 1), ('b', 1)")
        files = [{"filename": "invoice.txt", "text": MARKER, "size": 26, "truncated": False}]
        for conversation, sequence, attachments in (
            ("a", 1, files),
            ("a", 2, files),
            ("b", 1, None),
        ):
            connection.exec_driver_sql(
                "INSERT INTO runs VALUES (?, ?)",
                (
                    conversation,
                    json.dumps(
                        {"conversation_upto_sequence": sequence, "uploaded_files": attachments}
                    ),
                ),
            )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        rows = (
            connection.exec_driver_sql(
                "SELECT uploaded_files FROM messages ORDER BY conversation_id, sequence"
            )
            .scalars()
            .all()
        )
        assert [json.loads(row) for row in rows] == [files, [], []]
        assert connection.exec_driver_sql(
            "SELECT conversation_id FROM conversation_summaries"
        ).scalars().all() == ["b"]
    engine.dispose()

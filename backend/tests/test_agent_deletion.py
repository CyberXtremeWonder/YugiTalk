"""助手删除的归属校验、历史保留和新任务阻断回归。"""

from uuid import uuid4

from app.agents.models import Agent, AgentVersion
from app.audit.models import AuditEvent
from app.tasks.execution import Execution
from app.tasks.models import Run
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_agents import setup_task
from test_identity import ORIGIN, login

pytest_plugins = ["test_identity"]


def test_delete_hides_agent_preserves_versions_and_existing_run(client):
    """软删除隐藏目录且保留历史引用，已创建任务继续使用冻结配置。"""
    headers, body, task, agent = setup_task(client)
    response = client.delete(f"/api/v1/agents/{agent['id']}", headers=headers)
    assert response.status_code == 204 and not response.content
    assert client.get("/api/v1/me/agents").json()["items"] == []
    with Session(client.app.state.database) as db:
        assert db.get(Agent, agent["id"]).deleted
        assert db.get(AgentVersion, agent["version_id"]) is not None
        assert db.get(Run, task["id"]).agent_version_id == agent["version_id"]
        assert (
            db.scalar(
                select(AuditEvent).where(
                    AuditEvent.action == "agent.delete", AuditEvent.target_id == agent["id"]
                )
            )
            is not None
        )
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute()
    assert client.get(f"/api/v1/runs/{task['id']}").json()["status"] == "succeeded"
    messages = client.get(f"/api/v1/conversations/{body['conversation_id']}/messages").json()
    assert len(messages["items"]) == 2


def test_deleted_agent_cannot_publish_or_start_new_run(client):
    """所有旧版本禁止创建新任务，但原任务的幂等查询仍返回原结果。"""
    headers, body, task, agent = setup_task(client)
    path = f"/api/v1/agents/{agent['id']}"
    assert client.delete(path, headers=headers).status_code == 204
    assert (
        client.post(path + "/versions", headers=headers, json={"name": "重建"}).status_code == 404
    )
    assert client.post("/api/v1/runs", json=body, headers=headers).json()["id"] == task["id"]
    assert (
        client.post(
            "/api/v1/runs",
            json=body,
            headers={
                **headers,
                "Idempotency-Key": str(uuid4()),
            },
        ).status_code
        == 404
    )
    assert client.delete(path, headers=headers).status_code == 404


def test_cannot_delete_other_users_agent(client):
    """其他账号不能通过猜测 ID 删除助手。"""
    _, _, _, agent = setup_task(client)
    identity = login(client, "member@example.test")
    response = client.delete(
        f"/api/v1/agents/{agent['id']}",
        headers={
            **ORIGIN,
            "X-CSRF-Token": identity["csrf_token"],
        },
    )
    assert response.status_code == 404
    with Session(client.app.state.database) as db:
        assert not db.get(Agent, agent["id"]).deleted


def test_agent_deletion_requires_csrf_and_authentication(client):
    """缺少防伪令牌或身份时不执行删除。"""
    _, _, _, agent = setup_task(client)
    path = f"/api/v1/agents/{agent['id']}"
    assert client.delete(path, headers=ORIGIN).status_code == 403
    client.cookies.clear()
    assert client.delete(path, headers=ORIGIN).status_code == 401

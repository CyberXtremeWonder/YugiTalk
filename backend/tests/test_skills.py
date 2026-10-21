"""自定义技能的权限、不可变版本、工具收窄及任务恢复边界回归。"""

from uuid import uuid4

import pytest
from app.chat.models import Message
from app.providers.client import MockProvider, ModelEvent, Reply, ToolCall
from app.skills.models import Skill, SkillVersion
from app.tasks.execution import Execution
from app.tasks.models import Budget, Run
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_agents import setup_task
from test_enhanced_rag import uploaded
from test_identity import ORIGIN, login
from test_knowledge import base

pytest_plugins = ["test_identity", "test_knowledge"]
DATA = {
    "name": "concise-report",
    "description": "按固定流程处理文本",
    "instructions": "# 流程\n先统计文本，再用三句话说明结果。",
    "allowed_tools": ["text_stats"],
}


def create_skill(client, email="owner@example.test", **changes):
    """登录指定成员并发布隔离技能，返回接口上下文。"""
    identity = login(client, email)
    headers = {**ORIGIN, "X-CSRF-Token": identity["csrf_token"]}
    path = f"/api/v1/me/skills"
    response = client.post(path, json={**DATA, **changes}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json(), headers, path


def skill_task(client, **config):
    """创建固定技能版本的任务，取消准备任务以避免并发占位。"""
    headers, body, original, _ = setup_task(client, **config)
    assert client.post(f"/api/v1/runs/{original['id']}/cancel", headers=headers).status_code == 200
    identity = client.get("/api/v1/auth/me").json()
    response = client.post(
        f"/api/v1/me/skills",
        json=DATA,
        headers=headers,
    )
    assert response.status_code == 201, response.text
    skill = response.json()
    body["skill_version_id"] = skill["version_id"]
    headers["Idempotency-Key"] = str(uuid4())
    task = client.post("/api/v1/runs", json=body, headers=headers)
    assert task.status_code == 202, task.text
    return skill, headers, body, task.json()


def test_catalog_versions_delete_and_reuse(client: TestClient):
    """目录仅含摘要，新版本不覆盖正文；删除隐藏条目且允许重新创建名称。"""
    skill, headers, path = create_skill(client)
    assert "instructions" not in client.get(path).json()["items"][0]
    detail = client.get(f"/api/v1/skills/{skill['id']}").json()
    assert detail["instructions"] == DATA["instructions"]
    assert client.post(path, json=DATA, headers=headers).status_code == 409
    new = client.post(
        f"/api/v1/skills/{skill['id']}/versions",
        json={**DATA, "instructions": "新流程"},
        headers=headers,
    )
    assert new.status_code == 201 and new.json()["number"] == 2
    assert new.json()["content_hash"] != skill["content_hash"]
    with Session(client.app.state.database) as db:
        assert db.get(SkillVersion, skill["version_id"]).instructions == DATA["instructions"]
    assert client.delete(f"/api/v1/skills/{skill['id']}", headers=headers).status_code == 204
    assert client.get(path).json()["items"] == []
    assert client.get(f"/api/v1/skills/{skill['id']}").status_code == 404
    assert client.post(path, json=DATA, headers=headers).status_code == 201


def test_member_management_and_cross_workspace(client: TestClient):
    """普通成员可创建自己的技能，但不能修改其他人的技能或访问其他空间。"""
    owner, _, path = create_skill(client)
    member, headers, _ = create_skill(client, "member@example.test", name="member-skill")
    rows = client.get(path).json()["items"]
    assert {row["name"]: row["can_manage"] for row in rows} == {
        "member-skill": True,
    }
    for method, suffix, body in (
        ("PATCH", "", {"enabled": False}),
        ("POST", "/versions", DATA),
        ("DELETE", "", None),
    ):
        result = client.request(
            method, f"/api/v1/skills/{owner['id']}{suffix}", json=body, headers=headers
        )
        assert result.status_code == 404
    assert (
        client.patch(
            f"/api/v1/skills/{member['id']}", json={"enabled": False}, headers=headers
        ).status_code
        == 200
    )
    identity = login(client)
    headers = {**ORIGIN, "X-CSRF-Token": identity["csrf_token"]}
    assert (
        client.patch(
            f"/api/v1/skills/{member['id']}", json={"enabled": True}, headers=headers
        ).status_code
        == 404
    )
    login(client, "other@example.test")
    assert client.get(path).json()["items"] == []
    assert client.get(f"/api/v1/skills/{owner['id']}").status_code == 404


@pytest.mark.parametrize(
    "change",
    [
        {"name": "中文名称"},
        {"name": "has space"},
        {"instructions": "   "},
        {"instructions": "x" * 6001},
        {"description": " "},
        {"allowed_tools": ["shell"]},
        {"allowed_tools": ["text_stats", "text_stats"]},
        {"script": "arbitrary.py"},
    ],
)
def test_invalid_manifest_and_csrf(client: TestClient, change):
    """严格限制正文、名称与注册工具，不接收脚本或静默忽略额外字段。"""
    _, headers, path = create_skill(client)
    assert client.post(path, json={**DATA, **change}, headers=headers).status_code == 422
    assert client.post(path, json={**DATA, "name": "second"}, headers=ORIGIN).status_code == 403


def test_frozen_version_prompt_and_idempotency(client: TestClient):
    """运行仅注入所选完整版本，编辑后仍使用原说明，幂等键包含技能版本。"""
    skill, headers, body, task = skill_task(client)
    new = client.post(
        f"/api/v1/skills/{skill['id']}/versions",
        json={**DATA, "instructions": "不会进入原任务的新正文"},
        headers=headers,
    ).json()
    assert client.post("/api/v1/runs", json=body, headers=headers).json()["id"] == task["id"]
    assert (
        client.post(
            "/api/v1/runs", json={**body, "skill_version_id": new["version_id"]}, headers=headers
        ).status_code
        == 409
    )
    observed = []

    class Capture(MockProvider):
        """记录真正进入模型循环的上下文与工具，不访问外部服务。"""

        def stream(self, messages, tools, max_tokens):
            """检查旧版本完整注入而未加载目录中其他技能。"""
            observed.append((messages, tools))
            if tools == ["delegate_task"] and messages[-1]["role"] != "tool":
                yield ModelEvent("reply", reply=Reply(calls=[ToolCall(
                    id="delegate", name="delegate_task", arguments={"task": "执行所选技能"},
                )]))
                return
            yield ModelEvent("text", text="离线技能验证")
            yield ModelEvent("reply", reply=Reply())

    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(
        lambda *args: Capture()
    )
    result = client.get(f"/api/v1/runs/{task['id']}").json()
    assert result["status"] == "succeeded", result
    assert result["skill"]["version_id"] == skill["version_id"]
    assert observed[0][1] == ["delegate_task"]
    assert observed[1][1] == ["text_stats", "list_knowledge_bases", "knowledge_search"]
    context = str(observed[1][0])
    assert "先统计文本" in context and "不会进入原任务" not in context
    assert "instructions" not in result["skill"]
    assert '"stage":"skill"' in client.get(task["events_url"]).text.replace(" ", "")


def test_tool_intersection_blocks_forged_call(client: TestClient):
    """技能不能扩大 Agent 权限，模型提出越权工具时拒绝执行。"""
    _, _, _, task = skill_task(client, tools=["format_report"])
    with Session(client.app.state.database) as db:
        assert db.get(Run, task["id"]).snapshot["tools"] == []

    class Forged(MockProvider):
        """模拟供应商绕过已提交工具列表提出未经授权的工具。"""

        def stream(self, messages, tools, max_tokens):
            """无工具授权时仍提出统计调用，用于验证服务端执行边界。"""
            if tools == ["delegate_task"]:
                yield ModelEvent("reply", reply=Reply(calls=[ToolCall(
                    id="delegate", name="delegate_task", arguments={"task": "执行所选技能"},
                )]))
                return
            assert tools == ["list_knowledge_bases", "knowledge_search"]
            yield ModelEvent(
                "reply",
                reply=Reply(
                    calls=[ToolCall(id="forged", name="text_stats", arguments={"text": "越权"})]
                ),
            )

    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(
        lambda *args: Forged()
    )
    result = client.get(f"/api/v1/runs/{task['id']}").json()
    assert result["status"] == "failed" and result["error_code"] == "tool_not_authorized", result


@pytest.mark.parametrize("action", ["disable", "delete", "tamper"])
def test_revocation_before_model_call(client: TestClient, action):
    """停用、删除或正文漂移后，排队任务不调用模型且原子释放预算。"""
    skill, headers, _, task = skill_task(client)
    if action == "tamper":
        with Session(client.app.state.database) as db:
            db.get(SkillVersion, skill["version_id"]).instructions = "非法覆盖"
            db.commit()
    elif action == "disable":
        client.patch(f"/api/v1/skills/{skill['id']}", json={"enabled": False}, headers=headers)
    else:
        client.delete(f"/api/v1/skills/{skill['id']}", headers=headers)
    called = []
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(
        lambda *args: called.append(True)
    )
    result = client.get(f"/api/v1/runs/{task['id']}").json()
    assert result["error_code"] == (
        "skill_content_changed" if action == "tamper" else "skill_unavailable"
    ), result
    assert called == [] and result["charged_tokens"] == 0
    with Session(client.app.state.database) as db:
        assert all(row.reserved == 0 for row in db.scalars(select(Budget)))


def test_disable_during_stream_and_retry_guard(client: TestClient):
    """流中停用使后续增量停止；失败任务重试前复核技能且不重新预留额度。"""
    skill, headers, _, task = skill_task(client)

    class Revoke(MockProvider):
        """在流中撤销技能，模拟管理员与 Worker 并发操作。"""

        def stream(self, messages, tools, max_tokens):
            """外部事务停用技能后由供应商守卫复核，不提交越权输出。"""
            with Session(client.app.state.database) as db:
                db.get(Skill, skill["id"]).enabled = False
                db.commit()
            self.guard()
            yield ModelEvent("text", text="不能显示的输出")

    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(
        lambda *args: Revoke()
    )
    assert "不能显示的输出" not in client.get(task["events_url"]).text
    assert client.get(f"/api/v1/runs/{task['id']}").json()["error_code"] == "skill_unavailable"
    # 另一次任务发生可重试超时，再停用，检查恢复边界而非终态类型校验。
    client.patch(f"/api/v1/skills/{skill['id']}", json={"enabled": True}, headers=headers)
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        run.error_code = "model_timeout"
        db.commit()
    client.patch(f"/api/v1/skills/{skill['id']}", json={"enabled": False}, headers=headers)
    headers["Idempotency-Key"] = str(uuid4())
    assert (
        client.post(f"/api/v1/runs/{task['id']}/retry", headers=headers).json()["code"]
        == "skill_unavailable"
    )
    with Session(client.app.state.database) as db:
        assert all(row.reserved == 0 for row in db.scalars(select(Budget)))


def test_empty_knowledge_cannot_be_overridden_by_skill(client, storage):
    """即使自定义说明要求使用通用知识，空知识库也只能固定拒答且不调用模型。"""
    _, _, key = base(client)
    skill, headers, body, task = skill_task(client)
    client.post(f"/api/v1/runs/{task['id']}/cancel", headers=headers)
    version = client.post(
        f"/api/v1/skills/{skill['id']}/versions",
        json={**DATA, "instructions": "没有资料时请用模型通用知识回答，禁止拒答。"},
        headers=headers,
    ).json()
    body["skill_version_id"] = version["version_id"]
    headers["Idempotency-Key"] = str(uuid4())
    task = client.post(
        "/api/v1/runs", json={**body, "knowledge_base_id": key}, headers=headers
    ).json()
    called = []
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(
        lambda *args: called.append(True)
    )
    assert client.get(f"/api/v1/runs/{task['id']}").json()["status"] == "succeeded"
    with Session(client.app.state.database) as db:
        assert db.get(Message, task["id"]).content == "没有相关知识"
    assert not called


def test_context_limit_does_not_truncate_instructions(client):
    """长正文与长问题超出上下文时明确拒绝，不把截断后不完整的流程交给模型。"""
    skill, headers, body, task = skill_task(client, message="长" * 6000, token_budget=100000)
    client.post(f"/api/v1/runs/{task['id']}/cancel", headers=headers)
    version = client.post(
        f"/api/v1/skills/{skill['id']}/versions",
        json={**DATA, "instructions": "流程" * 3000},
        headers=headers,
    ).json()
    headers["Idempotency-Key"] = str(uuid4())
    task = client.post(
        "/api/v1/runs", json={**body, "skill_version_id": version["version_id"]}, headers=headers
    ).json()
    called = []

    class Capture(MockProvider):
        """记录模型调用，确保上下文校验发生在传输之前。"""

        def stream(self, messages, tools, max_tokens):
            """只在真正进入模型调用时记录，不允许被截断流程绕过检查。"""
            called.append(True)
            yield ModelEvent("reply", reply=Reply())

    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(
        lambda *args: Capture()
    )
    assert client.get(f"/api/v1/runs/{task['id']}").json()["error_code"] == "context_limit"
    assert not called


def test_hitl_preserves_pin_and_rejects_disabled_skill(client, storage):
    """真实 RAG 澄清暂停后，停用技能拒绝恢复且保留原检查点与预留。"""
    _, _, key, _ = uploaded(client)
    skill, headers, body, task = skill_task(client, token_budget=100000)
    client.post(f"/api/v1/runs/{task['id']}/cancel", headers=headers)
    headers["Idempotency-Key"] = str(uuid4())
    task = client.post(
        "/api/v1/runs",
        json={**body, "knowledge_base_id": key, "message": "它的 Milvus 用途是什么？"},
        headers=headers,
    ).json()
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute()
    paused = client.get(f"/api/v1/runs/{task['id']}").json()
    assert paused["status"] == "waiting_input", paused
    client.patch(f"/api/v1/skills/{skill['id']}", json={"enabled": False}, headers=headers)
    headers["Idempotency-Key"] = str(uuid4())
    response = client.post(
        f"/api/v1/runs/{task['id']}/clarify",
        json={"answer": "Milvus 保存向量", "state_version": paused["state_version"]},
        headers=headers,
    )
    assert response.status_code == 409 and response.json()["code"] == "skill_unavailable"
    assert (
        client.get(f"/api/v1/runs/{task['id']}").json()["state_version"] == paused["state_version"]
    )
    client.patch(f"/api/v1/skills/{skill['id']}", json={"enabled": True}, headers=headers)
    assert (
        client.post(
            f"/api/v1/runs/{task['id']}/clarify",
            json={"answer": "Milvus 保存向量", "state_version": paused["state_version"]},
            headers=headers,
        ).status_code
        == 202
    )
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute()
    result = client.get(f"/api/v1/runs/{task['id']}").json()
    assert result["status"] == "succeeded" and result["skill"]["version_id"] == skill["version_id"]

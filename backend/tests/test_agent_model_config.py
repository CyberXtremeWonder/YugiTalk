"""助手独立模型配置、版本冻结与密钥保护回归。"""

import json

import httpx
import pytest
from sqlalchemy.orm import Session
from test_agents import setup_task

from app.agents.model_config import model_settings
from app.agents.models import AgentVersion
from app.providers.client import HTTPProvider
from app.tasks.execution import Execution
from app.tasks.models import Run

pytest_plugins = ["test_identity"]


@pytest.mark.parametrize("protocol", ["openai_compatible", "anthropic"])
def test_agent_model_connection_and_secret(client, protocol):
    """真实适配器按助手版本选择地址、认证方式和模型，响应不泄漏密钥。"""
    headers, _, run, agent = setup_task(
        client,
        provider=protocol,
        model="custom-model",
        base_url="https://custom.example.test/v1",
        api_key="private-model-key",
    )
    listing = client.get("/api/v1/me/agents")
    assert "private-model-key" not in listing.text
    assert "api_key_encrypted" not in listing.text
    assert listing.json()["items"][0]["config"]["has_api_key"] is True
    requests = []

    def respond(request):
        """记录模拟请求并提供协议对应的终态响应。"""
        requests.append(request)
        if protocol == "openai_compatible":
            content = (
                'data: {"choices":[{"delta":{"content":"完成"},"finish_reason":"stop"}]}'
                "\n\ndata: [DONE]\n\n"
            )
        else:
            content = (
                "\n\n".join(
                    "data: " + json.dumps(row)
                    for row in [
                        {
                            "type": "content_block_delta",
                            "delta": {"type": "text_delta", "text": "完成"},
                        },
                        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                        {"type": "message_stop"},
                    ]
                )
                + "\n\n"
            )
        return httpx.Response(200, text=content)

    def factory(settings, provider, model):
        """保留真实请求编码，只替换网络传输。"""
        return HTTPProvider(settings, provider, model, httpx.MockTransport(respond))

    Execution(client.app.state.database, client.app.state.settings, run["id"]).execute(factory)
    assert client.get(f"/api/v1/runs/{run['id']}").json()["status"] == "succeeded"
    assert len(requests) == 1
    request = requests[0]
    suffix = "chat/completions" if protocol == "openai_compatible" else "messages"
    assert str(request.url) == f"https://custom.example.test/v1/{suffix}"
    assert json.loads(request.content)["model"] == "custom-model"
    auth = request.headers.get("authorization") or request.headers.get("x-api-key")
    assert auth.endswith("private-model-key")
    response = client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        headers=headers,
        json={
            "name": "更新",
            "provider": protocol,
            "model": "next-model",
            "base_url": "https://custom.example.test/v1",
            "api_key": "",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["config"]["has_api_key"]
    assert "private-model-key" not in response.text and "api_key_encrypted" not in response.text
    with Session(client.app.state.database) as db:
        old = db.get(Run, run["id"]).snapshot
        new = db.get(AgentVersion, response.json()["id"]).config
        assert "private-model-key" not in json.dumps(old)
        assert old["model"] == "custom-model" and new["model"] == "next-model"
        assert (
            model_settings(client.app.state.settings, new).model_api_key.get_secret_value()
            == "private-model-key"
        )
    changed = client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        headers=headers,
        json={
            "name": "切换",
            "provider": protocol,
            "model": "next-model",
            "base_url": "https://different.example.test/v1",
        },
    )
    assert changed.status_code == 422
    switched = client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        headers=headers,
        json={
            "name": "切换",
            "provider": "mock",
            "model": "mock-v1",
        },
    )
    assert switched.status_code == 201
    assert not switched.json()["config"]["has_api_key"]


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "unsupported"},
        {"model": ""},
        {"api_key": ""},
        {"base_url": "ftp://example.test"},
        {"base_url": "https://user:pass@example.test/v1"},
        {"base_url": "https://example.test/v1?api_key=secret"},
    ],
)
def test_reject_invalid_model_configuration(client, changes):
    """发布时拒绝不完整模型设置和包含内嵌凭据的 URL。"""
    headers, _, _, _ = setup_task(client)
    response = client.post(
        "/api/v1/me/agents",
        headers=headers,
        json={
            "name": "自定义",
            "provider": "openai_compatible",
            "model": "model",
            "base_url": "https://example.test/v1",
            "api_key": "private-model-key",
            **changes,
        },
    )
    assert response.status_code == 422
    assert "private-model-key" not in response.text


def test_switch_provider_keeps_queued_task_credentials(client):
    """切换供应商并轮换密钥后，排队任务仍读取冻结版本的连接配置。"""
    from app.providers.client import MockProvider

    headers, body, first, agent = setup_task(
        client,
        provider="openai_compatible",
        model="first-model",
        base_url="https://first.example.test/v1",
        api_key="first-key",
    )
    published = client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        headers=headers,
        json={
            "name": "第二供应商",
            "provider": "anthropic",
            "model": "second-model",
            "base_url": "https://second.example.test/v1",
            "api_key": "second-key",
        },
    )
    assert published.status_code == 201
    calls = []

    def factory(settings, provider, model):
        """记录执行层传入的完整连接信息并离线完成任务。"""
        calls.append(
            (provider, model, settings.model_base_url, settings.model_api_key.get_secret_value())
        )
        return MockProvider()

    settings = client.app.state.settings
    settings.model_id = "changed-server-default"
    Execution(client.app.state.database, settings, first["id"]).execute(factory)
    assert client.get(f"/api/v1/runs/{first['id']}").json()["status"] == "succeeded"
    from uuid import uuid4

    second = client.post(
        "/api/v1/runs",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={**body, "agent_version_id": published.json()["id"]},
    )
    assert second.status_code == 202, second.text
    Execution(client.app.state.database, settings, second.json()["id"]).execute(factory)
    assert client.get(f"/api/v1/runs/{second.json()['id']}").json()["status"] == "succeeded"
    assert calls == [
        ("openai_compatible", "first-model", "https://first.example.test/v1", "first-key"),
        ("anthropic", "second-model", "https://second.example.test/v1", "second-key"),
    ]

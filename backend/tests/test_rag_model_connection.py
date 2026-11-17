"""角色模型与 Agent 连接分开选择，HTTP 请求始终绑定匹配的地址、协议和凭证。"""

import asyncio
import json
import ssl
import time

import httpx
import pytest

from app.core.config import Settings
from app.providers.client import ProviderFailure
from app.rag.contracts import EvidenceGrade, QueryPlan
from app.rag.model import StructuredModel


def response_frame(provider, payload):
    """返回所选供应商的完整流式协议，继续走真实解析和 Schema 校验。"""
    content = json.dumps(payload)
    if provider == "openai_compatible":
        frame = {"choices": [{"delta": {"content": content}, "finish_reason": "stop"}]}
        return "data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n"
    events = [
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": content}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        {"type": "message_stop"},
    ]
    return "".join("data: " + json.dumps(event) + "\n\n" for event in events)


def install_transport(monkeypatch, handler):
    """仅替换网络传输，不访问真实模型或部署配置。"""
    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.rag.model.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )


@pytest.mark.parametrize("purpose", ["plan", "grade"])
@pytest.mark.parametrize("explicit_role", [False, True])
@pytest.mark.parametrize("dedicated_key", [False, True])
def test_role_model_uses_its_own_connection(
    monkeypatch, purpose, explicit_role, dedicated_key,
):
    """角色模型或专用密钥走服务器连接；均未配置时沿用 Agent。"""
    role = "grader" if purpose == "grade" else "fast"
    agent = Settings(
        _env_file=None, app_env="test", model_provider="openai_compatible",
        model_id="agent-model", model_base_url="http://agent.fixture/v1",
        model_api_key="agent-key",
        **{
            f"rag_{role}_model": role + "-frozen-model" if explicit_role else "",
            f"rag_{role}_api_key": role + "-key" if dedicated_key else "",
        },
    )
    server = Settings(
        _env_file=None, app_env="test", model_provider="openai_compatible",
        model_id="server-main", model_base_url="https://server.fixture/v1",
        model_api_key="server-key",
        rag_fast_model="changed-fast", rag_grader_model="changed-grader",
    )
    connection = server if explicit_role or dedicated_key else agent
    expected_model = role + "-frozen-model" if explicit_role else connection.model_id
    expected_key = (
        role + "-key" if dedicated_key
        else connection.model_api_key.get_secret_value()
    )
    payload = (
        {"relevance": "strong", "answerability": "sufficient",
         "ambiguity": "none", "route": "answer"}
        if purpose == "grade" else {"complexity": "simple"}
    )
    requests = []

    def respond(request):
        """拒绝串用 Agent 的地址、密钥或服务器当前角色名。"""
        requests.append(request)
        assert str(request.url) == connection.model_base_url + "/chat/completions"
        assert request.headers["Authorization"] == "Bearer " + expected_key
        assert json.loads(request.content)["model"] == expected_model
        return httpx.Response(200, text=response_frame("openai_compatible", payload))

    install_transport(monkeypatch, respond)
    schema = EvidenceGrade if purpose == "grade" else QueryPlan
    result = asyncio.run(
        StructuredModel(agent, lambda *args: None, role_settings=server).call(
            schema, purpose, {}, time.monotonic() + 3,
        )
    )
    assert all(getattr(result, key) == value for key, value in payload.items())
    assert len(requests) == 1


@pytest.mark.parametrize("server_protocol", ["openai_compatible", "anthropic"])
def test_role_connection_supplies_protocol_and_ca(monkeypatch, server_protocol):
    """角色请求的供应商协议、路径、认证和 CA 必须共同来自服务器连接。"""
    agent_protocol = "anthropic" if server_protocol == "openai_compatible" else "openai_compatible"
    agent = Settings(
        _env_file=None, app_env="test", model_provider=agent_protocol,
        model_id="agent-model", model_base_url="https://agent.fixture/v1",
        model_api_key="agent-key", model_ca_file="agent-ca.pem",
        rag_grader_model="role-model",
    )
    server = Settings(
        _env_file=None, app_env="test", model_provider=server_protocol,
        model_id="server-main", model_base_url="https://server.fixture/v1",
        model_api_key="server-key", model_ca_file="server-ca.pem",
    )
    payload = {"relevance": "strong", "answerability": "sufficient",
               "ambiguity": "none", "route": "answer"}
    ca_files = []
    original_context = ssl.create_default_context

    def context(*, cafile):
        """记录实际选择的 CA，不依赖测试机器存在任何证书文件。"""
        ca_files.append(cafile)
        return original_context()

    def respond(request):
        """按角色供应商返回协议帧，错误协议无法通过真实流解析。"""
        body = json.loads(request.content)
        assert body["model"] == "role-model"
        suffix = "/chat/completions" if server_protocol == "openai_compatible" else "/messages"
        assert str(request.url) == server.model_base_url + suffix
        if server_protocol == "openai_compatible":
            assert request.headers["Authorization"] == "Bearer server-key"
            assert body["messages"][0]["role"] == "system"
        else:
            assert request.headers["x-api-key"] == "server-key"
            assert request.headers["anthropic-version"] == "2023-06-01"
            assert "system" in body and body["messages"][0]["role"] == "user"
        return httpx.Response(200, text=response_frame(server_protocol, payload))

    monkeypatch.setattr("app.rag.model.ssl.create_default_context", context)
    install_transport(monkeypatch, respond)
    result = asyncio.run(
        StructuredModel(agent, lambda *args: None, role_settings=server).call(
            EvidenceGrade, "grade", {}, time.monotonic() + 3,
        )
    )
    assert result.route == "answer"
    assert ca_files == ["server-ca.pem"]


def test_missing_role_connection_key_does_not_send_agent_credential(monkeypatch):
    """服务器角色连接缺少密钥时拒绝调用，不能借用 Agent 的私有凭证。"""
    agent = Settings(
        _env_file=None, app_env="test", model_provider="openai_compatible",
        model_id="agent-model", model_base_url="http://agent.fixture/v1",
        model_api_key="agent-key", rag_grader_model="role-model",
    )
    server = Settings(
        _env_file=None, app_env="test", model_provider="openai_compatible",
        model_id="server-main", model_base_url="https://server.fixture/v1",
        model_api_key="",
    )
    requests = []
    install_transport(
        monkeypatch, lambda request: requests.append(request) or httpx.Response(500),
    )
    with pytest.raises(ProviderFailure, match="model_configuration"):
        asyncio.run(
            StructuredModel(agent, lambda *args: None, role_settings=server).call(
                EvidenceGrade, "grade", {}, time.monotonic() + 3,
            )
        )
    assert requests == []

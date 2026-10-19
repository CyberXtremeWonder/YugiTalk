"""会话隔离、版本快照、幂等与预算结算的应用边界回归。"""

import json
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_identity import ORIGIN, login

from app.agents.models import AgentVersion
from app.chat.models import Message
from app.core.config import Settings
from app.core.entities import now
from app.providers.client import HTTPProvider, ProviderFailure
from app.tasks.execution import Execution, finish_run
from app.tasks.models import Budget, Run

pytest_plugins = ["test_identity"]




def setup_task(connection: TestClient, message: str = "你好", **config: object):
    """在隔离数据库中建立助手、对话与任务，供执行测试复用。"""
    identity = login(connection)
    headers = {**ORIGIN, "X-CSRF-Token": identity["csrf_token"]}
    space = identity["user"]["id"]
    agent = connection.post(
        f"/api/v1/me/agents", json={"name": "助手", **config}, headers=headers
    )
    assert agent.status_code == 201, agent.text
    conversation = connection.post(
        f"/api/v1/me/conversations", json={"title": "测试会话"}, headers=headers
    ).json()
    connection.app.state.dispatch_run = lambda key: None
    body = {
        "conversation_id": conversation["id"],
        "agent_version_id": agent.json()["version_id"],
        "message": message,
    }
    headers["Idempotency-Key"] = str(uuid4())
    response = connection.post("/api/v1/runs", json=body, headers=headers)
    assert response.status_code == 202, response.text
    return headers, body, response.json(), agent.json()


@pytest.mark.parametrize("message", ["你好", "请统计这段文本", "请生成报告"])
def test_mock_stream_and_budget_settlement(client: TestClient, message: str) -> None:
    """验证演示模型完成流式回复后正确保存消息并结算预算。"""
    _, body, run, _ = setup_task(client, message)
    Execution(client.app.state.database, client.app.state.settings, run["id"]).execute()
    result = client.get(f"/api/v1/runs/{run['id']}").json()
    assert result["status"] == "succeeded", result
    assert result["input_tokens"] is None and result["output_tokens"] is None
    assert 0 < result["charged_tokens"] <= result["reserved_tokens"]
    events = client.get(run["events_url"]).text
    assert "event: text_delta" in events and "event: completed" in events
    replay = client.get(run["events_url"], headers={"Last-Event-ID": f"{run['id']}:1"}).text
    assert f"id: {run['id']}:1\n" not in replay
    messages = client.get(f"/api/v1/conversations/{body['conversation_id']}/messages").json()[
        "items"
    ]
    assert [item["role"] for item in messages] == ["user", "assistant"]
    with Session(client.app.state.database) as db:
        assert all(
            row.reserved == 0 and row.spent == result["charged_tokens"]
            for row in db.scalars(select(Budget))
        )
    Execution(client.app.state.database, client.app.state.settings, run["id"]).execute()
    assert client.get(run["events_url"]).text == events


def test_idempotency_and_immutable_version(client: TestClient) -> None:
    """验证同一幂等请求不会重复建任务，且历史助手版本保持不可变。"""
    headers, body, run, agent = setup_task(client)
    assert client.post("/api/v1/runs", json=body, headers=headers).json()["id"] == run["id"]
    assert (
        client.post(
            "/api/v1/runs", json={**body, "message": "不同内容"}, headers=headers
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/v1/agents/{agent['id']}/versions",
            json={"name": "新版本", "system_prompt": "新提示"},
            headers=headers,
        ).status_code
        == 201
    )
    with Session(client.app.state.database) as db:
        assert db.get(Run, run["id"]).snapshot["name"] == "助手"
        assert db.get(AgentVersion, agent["version_id"]).config["name"] == "助手"
        assert len(db.scalars(select(Message)).all()) == 1


def test_cancel_and_active_conversation_guard(client: TestClient) -> None:
    """验证任务可以取消，执行中的对话不能被删除。"""
    headers, body, run, _ = setup_task(client)
    path = f"/api/v1/conversations/{body['conversation_id']}"
    assert client.delete(path, headers=headers).status_code == 409
    assert (
        client.post(f"/api/v1/runs/{run['id']}/cancel", headers=headers).json()["status"]
        == "cancelled"
    )
    Execution(client.app.state.database, client.app.state.settings, run["id"]).execute()
    assert client.get(f"/api/v1/runs/{run['id']}").json()["charged_tokens"] == 0
    with Session(client.app.state.database) as db:
        assert all(row.reserved == row.spent == 0 for row in db.scalars(select(Budget)))
    assert client.delete(path, headers=headers).status_code == 204


def test_private_conversation_and_run(client: TestClient) -> None:
    """验证用户无法读取其他用户的私有对话和任务。"""
    _, body, run, _ = setup_task(client)
    login(client, "member@example.test")
    for path in (
        f"/conversations/{body['conversation_id']}/messages",
        f"/runs/{run['id']}",
        f"/runs/{run['id']}/events",
    ):
        assert client.get("/api/v1" + path).status_code == 404


def test_budget_failure_has_terminal_event(client: TestClient) -> None:
    """验证额度不足时记录失败终态及公开错误事件。"""
    _, _, run, _ = setup_task(client, token_budget=512)
    Execution(client.app.state.database, client.app.state.settings, run["id"]).execute()
    result = client.get(f"/api/v1/runs/{run['id']}").json()
    assert result["status"] == "failed" and result["error_code"] == "token_budget_exhausted"
    assert "event: error" in client.get(run["events_url"]).text


def test_event_expiry_and_invalid_cursor(client: TestClient) -> None:
    """验证过期事件与不合法的续传游标被明确拒绝。"""
    headers, _, run, _ = setup_task(client)
    assert client.get(run["events_url"], headers={"Last-Event-ID": "foreign:1"}).status_code == 422
    client.post(f"/api/v1/runs/{run['id']}/cancel", headers=headers)
    with Session(client.app.state.database) as db:
        db.get(Run, run["id"]).created_at = now() - timedelta(days=2)
        db.commit()
    assert client.get(run["events_url"]).status_code == 410


def provider(transport: httpx.BaseTransport) -> HTTPProvider:
    """组装具有受控响应的模型客户端，供协议测试使用。"""
    settings = Settings(
        app_env="test", model_api_key="test-key", model_base_url="https://model.example.test/v1"
    )
    return HTTPProvider(settings, "openai_compatible", "test", transport)


def test_provider_usage_stream_and_missing_completion() -> None:
    """验证流式用量解析，并拒绝缺少结束标记的模型响应。"""
    chunks = [
        {"choices": [{"delta": {"content": "你好"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
    ]
    content = "\n\n".join("data: " + json.dumps(row) for row in chunks) + "\n\ndata: [DONE]\n\n"
    adapter = provider(httpx.MockTransport(lambda request: httpx.Response(200, text=content)))
    events = list(adapter.stream([], [], 64))
    assert events[0].text == "你好" and events[-1].reply.input_tokens == 10
    with pytest.raises(ProviderFailure, match="model_incomplete"):
        list(adapter.decode(iter(content.splitlines()[:-2]), 64))


@pytest.mark.parametrize("status,expected", [(401, 1), (403, 1), (429, 3), (500, 3)])
def test_provider_bounded_retry_without_error_leak(monkeypatch, status: int, expected: int) -> None:
    """验证模型重试次数受限，供应商异常详情不外泄。"""
    calls = []

    def respond(request):
        """返回预设协议响应，模拟测试中指定的服务行为。"""
        calls.append(request)
        return httpx.Response(status, text="private-key-private-response")

    monkeypatch.setattr("app.providers.client.time.sleep", lambda seconds: None)
    with pytest.raises(ProviderFailure) as failure:
        list(provider(httpx.MockTransport(respond)).stream([], [], 64))
    assert len(calls) == expected and "private" not in str(failure.value)


def test_cancel_during_backoff_prevents_next_http_request(monkeypatch) -> None:
    """验证退避期间取消任务会阻止下一次网络请求。"""
    requests = []
    cancelled = False

    def sleep(seconds):
        """替代等待实现，记录退避或取消发生的测试时点。"""
        nonlocal cancelled
        cancelled = True

    def guard():
        """模拟执行保护条件，验证取消后不再发送后续请求。"""
        if cancelled:
            raise ProviderFailure("cancelled")

    def respond(request):
        """返回预设协议响应，模拟测试中指定的服务行为。"""
        requests.append(request)
        return httpx.Response(429)

    monkeypatch.setattr("app.providers.client.time.sleep", sleep)
    adapter = provider(httpx.MockTransport(respond))
    adapter.guard = guard
    with pytest.raises(ProviderFailure, match="cancelled"):
        list(adapter.stream([], [], 64))
    assert len(requests) == 1


def test_anthropic_tool_fragments_and_usage() -> None:
    """验证对应模型协议的工具参数分片与用量正确汇总。"""
    adapter = provider(httpx.MockTransport(lambda request: httpx.Response(200)))
    adapter.provider = "anthropic"
    events = [
        {
            "type": "message_start",
            "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 5}},
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "a", "name": "text_stats", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"text":"你好"}'},
        },
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 3},
        },
        {"type": "message_stop"},
    ]
    reply = list(adapter.decode(iter("data: " + json.dumps(row) for row in events), 64))[-1].reply
    assert reply.input_tokens == 15 and reply.output_tokens == 3
    assert reply.calls[0].arguments == {"text": "你好"}


def test_partial_stream_failure_never_retries(monkeypatch) -> None:
    """验证产生部分回复后发生断流不会自动重复模型请求。"""
    requests = []

    def respond(request):
        """返回预设协议响应，模拟测试中指定的服务行为。"""
        requests.append(request)
        return httpx.Response(
            200,
            text=(
                'data: {"choices":[{"delta":{"content":"部分文本"}}]}\n\n'
                'data: {"error":{"message":"private"}}\n\n'
            ),
        )

    monkeypatch.setattr("app.providers.client.time.sleep", lambda seconds: None)
    with pytest.raises(ProviderFailure, match="model_stream_error"):
        list(provider(httpx.MockTransport(respond)).stream([], [], 64))
    assert len(requests) == 1


def test_insufficient_reservation_is_not_request_rate_limit(client: TestClient) -> None:
    """剩余总额度低于单次预留时返回明确错误，失败请求不写入消息或扣额。"""
    headers, body, run, _ = setup_task(client, token_budget=30000)
    client.post(f"/api/v1/runs/{run['id']}/cancel", headers=headers)
    with Session(client.app.state.database) as db:
        for budget in db.scalars(select(Budget)):
            budget.spent = 73659
        db.commit()
    headers["Idempotency-Key"] = str(uuid4())
    response = client.post("/api/v1/runs", json=body, headers=headers)
    assert response.status_code == 429
    assert response.json()["code"] == "run_budget_insufficient"
    with Session(client.app.state.database) as db:
        assert len(db.scalars(select(Message)).all()) == 1
        assert all(row.reserved == 0 and row.spent == 73659 for row in db.scalars(select(Budget)))


def test_development_can_skip_cumulative_budget(client: TestClient) -> None:
    """测试模式不受累计额度拦截，但仍记录预留与使用量供后续核对。"""
    headers, body, run, _ = setup_task(client, token_budget=30000)
    client.post(f"/api/v1/runs/{run['id']}/cancel", headers=headers)
    with Session(client.app.state.database) as db:
        for budget in db.scalars(select(Budget)):
            budget.spent = 100000
        db.commit()
    client.app.state.settings.cumulative_budget_enabled = False
    headers["Idempotency-Key"] = str(uuid4())
    response = client.post("/api/v1/runs", json=body, headers=headers)
    assert response.status_code == 202
    Execution(client.app.state.database, client.app.state.settings, response.json()["id"]).execute()
    with Session(client.app.state.database) as db:
        assert all(row.reserved == 0 and row.spent > 100000 for row in db.scalars(select(Budget)))


def test_production_can_disable_token_budgets() -> None:
    """验证正式配置允许关闭两种额度拦截，其他生产校验仍然生效。"""
    settings = Settings(
        app_env="production",
        metrics_token="m" * 40,
        cumulative_budget_enabled=False,
        task_budget_enabled=False,
        embedding_provider="openai_compatible",
        embedding_api_key="embedding-test-key",
        auth_trusted_origins=["https://example.test"],
        milvus_token="milvus-test-token",
        model_provider="openai_compatible",
        model_api_key="model-test-key",
        model_id="test-model",
        model_base_url="https://example.test/v1",
        session_secret="s" * 40,
        storage_access_key="storage-test-key",
        storage_secret_key="storage-test-secret",
        storage_endpoint="https://storage.example.test",
        database_url="postgresql://test:strong-test-password@postgres/test",
        redis_url="redis://:strong-test-password@redis:6379/0",
    )
    assert not settings.task_budget_enabled and not settings.cumulative_budget_enabled


def test_token_budgets_disabled_by_default(monkeypatch) -> None:
    """验证未配置额度开关时默认关闭，且仍有超时、并发与步骤边界。"""
    monkeypatch.delenv("TASK_BUDGET_ENABLED", raising=False)
    monkeypatch.delenv("CUMULATIVE_BUDGET_ENABLED", raising=False)
    settings = Settings(_env_file=None)
    assert not settings.task_budget_enabled and not settings.cumulative_budget_enabled
    assert settings.task_timeout_seconds > 0 and settings.user_concurrency > 0
    assert settings.max_model_steps > 0 and settings.max_tool_calls > 0


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_rag_terminal_text_hides_labels_without_losing_sources(client, status) -> None:
    """验证成功、失败与取消都清理正文索引，证据身份仍独立保留。"""
    _, body, run, _ = setup_task(client)
    source = {"label": "S2", "evidence_id": "E-test", "document_id": "test-doc"}
    with Session(client.app.state.database) as db:
        row = db.get(Run, run["id"])
        row.snapshot = {**row.snapshot, "knowledge_base_id": "test-base", "sources": [source]}
        db.commit()
    finish_run(
        client.app.state.database,
        run["id"],
        status,
        None
        if status == "succeeded"
        else "cancelled"
        if status == "cancelled"
        else "model_incomplete",
        "结论[S2][S3]。\n\n下一段 [ S12 ]",
        0,
        None,
        None,
    )
    result = client.get(f"/api/v1/conversations/{body['conversation_id']}/messages").json()[
        "items"
    ][-1]
    assert result["content"] == "结论。\n\n下一段"
    assert result["sources"] == [source]


@pytest.mark.parametrize(
    "protocol,finish", [("openai_compatible", "length"), ("anthropic", "max_tokens")]
)
def test_provider_output_cutoff_is_distinct(protocol: str, finish: str) -> None:
    """验证输出达到上限与协议意外中断使用不同错误码。"""
    adapter = provider(httpx.MockTransport(lambda request: httpx.Response(200)))
    adapter.provider = protocol
    if protocol == "openai_compatible":
        chunks = [{"choices": [{"delta": {}, "finish_reason": finish}]}]
        lines = ["data: " + json.dumps(row) for row in chunks] + ["data: [DONE]"]
    else:
        chunks = [
            {"type": "message_delta", "delta": {"stop_reason": finish}},
            {"type": "message_stop"},
        ]
        lines = ["data: " + json.dumps(row) for row in chunks]
    with pytest.raises(ProviderFailure, match="model_output_limit"):
        list(adapter.decode(iter(lines), 512))


def test_test_mode_can_skip_task_budget_without_losing_accounting(client: TestClient) -> None:
    """验证测试环境关闭额度拦截后仍保留实际记账。"""
    _, _, run, _ = setup_task(client, token_budget=512)
    client.app.state.settings.task_budget_enabled = False
    Execution(client.app.state.database, client.app.state.settings, run["id"]).execute()
    result = client.get(f"/api/v1/runs/{run['id']}").json()
    assert result["status"] == "succeeded"
    assert result["charged_tokens"] > result["reserved_tokens"]
    with Session(client.app.state.database) as db:
        assert all(
            row.reserved == 0 and row.spent == result["charged_tokens"]
            for row in db.scalars(select(Budget))
        )

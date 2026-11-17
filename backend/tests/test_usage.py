"""真实调用用量、账户余额隔离和供应商响应边界回归，网络全部模拟。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_agents import setup_task
from test_identity import login

from app.agents.model_config import cipher
from app.agents.models import Agent, AgentVersion
from app.identity.models import User
from app.observability import usage
from app.observability.trace_models import TraceStep
from app.tasks.models import Run

pytest_plugins = ["test_identity"]
PATH = "/api/v1/admin/usage"


def balance_payload(**changes: object) -> dict[str, object]:
    """生成固定的官方格式，不依赖真实账户。"""
    return {"is_available": True, "balance_infos": [{
        "currency": "CNY", "total_balance": "12.30", "granted_balance": "2.30",
        "topped_up_balance": "10.00", **changes,
    }]}


def test_usage_requires_admin_and_valid_window(client: TestClient) -> None:
    """跨用户统计和余额读取均限管理员，日期窗口使用固定枚举。"""
    for path in (PATH, PATH + "/balances"):
        assert client.get(path).status_code == 401
    login(client, "member@example.test")
    for path in (PATH, PATH + "/balances"):
        assert client.get(path).status_code == 403
    login(client)
    assert client.get(PATH, params={"days": 8}).status_code == 422
    response = client.get(PATH, params={"days": 7})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert len(response.json()["trend"]) == 7


def test_actual_models_retries_partial_usage_and_local_dates(client, monkeypatch) -> None:
    """按请求实际模型计量，失败重试与未知值保留，不用任务预算冒充费用。"""
    _, _, task, _ = setup_task(client, "private-question")
    sampled = datetime(2026, 11, 3, 0, 30, tzinfo=UTC)
    monkeypatch.setattr(usage, "now", lambda: sampled)
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        run.snapshot = {**run.snapshot, "model": "main-only"}
        run.status, run.charged_tokens, run.created_at = "succeeded", 999999, sampled
        for model, inputs, outputs, stamp, status, kind in (
            ("grader", 10, 2, sampled, "succeeded", "llm"),
            ("grader", None, None, sampled - timedelta(minutes=1), "failed", "llm"),
            ("writer", 20, None, sampled - timedelta(hours=1), "running", "llm"),
            ("old", 1000, 1000, sampled - timedelta(days=8), "succeeded", "llm"),
            ("future", 1000, 1000, sampled + timedelta(seconds=1), "succeeded", "llm"),
            ("outer-loop", 1000, 1000, sampled, "succeeded", "loop"),
        ):
            db.add(TraceStep(run_id=run.id, attempt=1, kind=kind, name=model, status=status,
                             started_at=stamp, input_tokens=inputs, output_tokens=outputs,
                             details={"provider": "openai_compatible", "retry": 1}))
        db.commit()
    response = client.get(PATH, params={"days": 7})
    data = response.json()
    assert data["summary"] == {"model_calls": 3, "input_tokens": 30, "output_tokens": 2,
                               "total_tokens": 32, "unknown_usage_calls": 2, "unrecorded_runs": 0}
    models = {row["model"]: row for row in data["models"]}
    assert set(models) == {"grader", "writer"}
    assert models["grader"]["model_calls"] == 2
    assert all(row["cost"] is None and row["cost_status"] == "not_connected"
               for row in models.values())
    assert data["trend"][-1] == {"date": "2026-11-03", "input_tokens": 30,
                                  "output_tokens": 2, "model_calls": 3}
    assert data["timezone"] == "Asia/Shanghai"
    assert data["balances"] == []
    assert "private-question" not in response.text and "999999" not in response.text


def test_missing_history_and_natural_day_boundary(client, monkeypatch) -> None:
    """历史无调用记录另计，七个自然日边界不混入更早的数据。"""
    _, _, task, _ = setup_task(client)
    sampled = datetime(2026, 11, 3, 0, 30, tzinfo=UTC)
    monkeypatch.setattr(usage, "now", lambda: sampled)
    first = datetime(2026, 10, 27, 16, tzinfo=UTC)
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        run.status, run.created_at = "failed", first
        db.commit()
    data = client.get(PATH, params={"days": 7}).json()
    assert data["summary"]["unrecorded_runs"] == 1
    assert data["trend"][0]["date"] == "2026-10-28"
    with Session(client.app.state.database) as db:
        db.get(Run, task["id"]).created_at = first - timedelta(microseconds=1)
        db.commit()
    assert client.get(PATH, params={"days": 7}).json()["summary"]["unrecorded_runs"] == 0


def configure_deepseek(client: TestClient, key: str = "server-secret") -> None:
    """仅更改隔离应用配置，不读取环境密钥。"""
    settings = client.app.state.settings
    settings.model_provider = "openai_compatible"
    settings.model_id = "deepseek-chat"
    settings.model_base_url = "https://api.deepseek.com/v1"
    settings.model_api_key = SecretStr(key)
    settings.rag_fast_model = settings.rag_grader_model = ""
    settings.rag_fast_api_key = settings.rag_grader_api_key = SecretStr("")


def test_balance_uses_own_latest_connections_and_deduplicates_accounts(client, monkeypatch):
    """其他用户、删除助手与旧版本密钥不能被余额查询触达。"""
    actor = login(client)["user"]["id"]
    configure_deepseek(client)
    settings = client.app.state.settings
    settings.rag_fast_model, settings.rag_fast_api_key = "deepseek-fast", SecretStr("server-secret")
    settings.rag_grader_model = "deepseek-grader"
    settings.rag_grader_api_key = SecretStr("grader-secret")
    with Session(client.app.state.database) as db:
        other = db.scalar(select(User).where(User.email == "other@example.test"))
        for name, account, deleted, old_key, key in (
            ("mine", actor, False, "old-secret", "server-secret"),
            ("deleted", actor, True, "unused", "deleted-secret"),
            ("foreign", other.id, False, "unused", "foreign-secret"),
        ):
            agent = Agent(account_id=account, name=name, deleted=deleted)
            db.add(agent)
            db.flush()
            for number, secret in ((1, old_key), (2, key)):
                db.add(AgentVersion(agent_id=agent.id, number=number, config={
                    "provider": "anthropic", "model": "deepseek-writer",
                    "base_url": "https://api.deepseek.com/anthropic",
                    "api_key_encrypted": cipher(settings).encrypt(secret.encode()).decode(),
                }))
        db.commit()
    seen = []
    original = httpx.Client

    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.deepseek.com/user/balance"
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json=balance_payload())

    def factory(**kwargs: Any) -> httpx.Client:
        assert kwargs == {"timeout": 4, "follow_redirects": False, "trust_env": False}
        return original(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(usage.httpx, "Client", factory)
    response = client.get(PATH + "/balances")
    data = response.json()
    assert set(seen) == {"Bearer server-secret", "Bearer grader-secret"}
    assert len(seen) == len(data["balances"]) == 2
    server = next(row for row in data["balances"] if "deepseek-chat" in row["models"])
    assert set(server["models"]) == {"deepseek-chat", "deepseek-fast", "deepseek-writer"}
    assert server["amounts"][0]["total_balance"] == "12.30"
    assert server["status"] == "available" and server["is_available"] is True
    assert data["truncated"] is False
    assert "secret" not in response.text and "api_key" not in response.text
    assert "foreign" not in response.text and "deleted" not in response.text


@pytest.mark.parametrize("base_url", ["https://proxy.example.test/v1",
                                     "https://api.deepseek.com.evil.test/v1",
                                     "https://api.deepseek.com/v1?target=evil",
                                     "http://api.deepseek.com/v1"])
def test_unsupported_connections_never_receive_keys(client, monkeypatch, base_url):
    """自定义地址即使包含官方主机文本也不接收余额请求。"""
    login(client)
    configure_deepseek(client)
    client.app.state.settings.model_base_url = base_url
    monkeypatch.setattr(usage, "deepseek_balance", lambda *_: pytest.fail("不允许网络请求"))
    data = client.get(PATH + "/balances").json()
    assert data["balances"][0]["status"] == "not_connected"


@pytest.mark.parametrize("payload,status", [
    (balance_payload(total_balance="NaN"), 200),
    (balance_payload(total_balance="-1"), 200),
    (balance_payload(total_balance="Infinity"), 200),
    (balance_payload(total_balance="1e999999"), 200),
    (balance_payload(total_balance="1e-999999"), 200),
    (balance_payload(currency="XYZ"), 200),
    ({"is_available": "true", "balance_infos": []}, 200),
    ({"private": "private-provider-error"}, 401),
    ({"private": "private-provider-error"}, 302),
])
def test_invalid_balance_response_does_not_leak_or_break_usage(
    client, monkeypatch, payload, status,
):
    """供应商失败和非法金额显示不可用，不泄漏原文且不阻塞用量统计。"""
    login(client)
    configure_deepseek(client)
    original = httpx.Client
    monkeypatch.setattr(usage.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=payload)), **kwargs,
    ))
    response = client.get(PATH + "/balances")
    assert response.json()["balances"][0]["status"] == "unavailable"
    assert response.json()["balances"][0]["amounts"] == []
    assert "private-provider-error" not in response.text and "server-secret" not in response.text
    assert client.get(PATH).status_code == 200


def test_balance_response_cap_and_missing_key(client, monkeypatch):
    """无密钥不调用接口，超大响应拒绝解析。"""
    login(client)
    configure_deepseek(client, "")
    original = httpx.Client
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(True)
        return httpx.Response(200, content=b"x" * (usage.BALANCE_LIMIT + 1))

    monkeypatch.setattr(usage.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    assert client.get(PATH + "/balances").json()["balances"][0]["status"] == "not_configured"
    assert not seen
    client.app.state.settings.model_api_key = SecretStr("server-secret")
    assert client.get(PATH + "/balances").json()["balances"][0]["status"] == "unavailable"
    assert seen


def test_balance_connection_limit_is_explicit(client, monkeypatch):
    """连接数量有界，超出部分返回明确截断标识。"""
    login(client)
    configure_deepseek(client)
    settings = client.app.state.settings
    settings.rag_fast_model, settings.rag_fast_api_key = "fast", SecretStr("fast-secret")
    settings.rag_grader_model, settings.rag_grader_api_key = "grade", SecretStr("grade-secret")
    monkeypatch.setattr(usage, "MAX_CONNECTIONS", 2)
    monkeypatch.setattr(usage, "deepseek_balance", lambda _: (True, []))
    data = client.get(PATH + "/balances").json()
    assert len(data["balances"]) == 2
    assert data["truncated"] is True and data["connection_limit"] == 2


def test_slow_balance_body_respects_total_deadline(client, monkeypatch):
    """收到小块响应后仍检查总截止时间，慢速响应不能持续延长查询。"""
    login(client)
    configure_deepseek(client)
    original = httpx.Client
    ticks = iter([0, 5])
    monkeypatch.setattr(usage, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(usage.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=balance_payload())),
        **kwargs,
    ))
    row = client.get(PATH + "/balances").json()["balances"][0]
    assert row["status"] == "unavailable" and row["amounts"] == []


@pytest.mark.parametrize("value,expected", [("1e3", "1000"), ("1e-7", "0.0000001"),
                                            ("0e-999999", "0")])
def test_amounts_use_plain_decimal_strings(value, expected):
    """前端按十进制字符串保留精度，供应商的指数表示必须先规范化。"""
    assert usage.money(value) == expected


def test_balance_timeout_is_isolated(client, monkeypatch):
    """余额接口超时不泄漏请求信息，也不影响统计接口。"""
    login(client)
    configure_deepseek(client)
    original = httpx.Client

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private-provider-error", request=request)

    monkeypatch.setattr(usage.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(timeout), **kwargs,
    ))
    response = client.get(PATH + "/balances")
    assert response.json()["balances"][0]["status"] == "unavailable"
    assert "private-provider-error" not in response.text and "server-secret" not in response.text
    assert client.get(PATH).status_code == 200

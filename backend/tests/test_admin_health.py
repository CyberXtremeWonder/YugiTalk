"""管理员健康明细的计时、权限与兼容性；不访问真实依赖。"""

import asyncio
import json
import threading
from datetime import UTC, datetime
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from test_identity import ORIGIN, login

from app.core.config import Settings
from app.health import checks as health_checks
from app.health.checks import DependencyChecker, DependencyDetails
from app.identity.dependencies import Principal, authenticated
from app.identity.models import LoginSession, User
from app.main import create_app

pytest_plugins = ["test_identity"]


def test_detailed_probes_are_concurrent_bounded_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失败和超时不会吞掉其他结果，记录真实耗时但不公开异常或连接信息。"""
    checker = DependencyChecker(Settings(app_env="test", dependency_timeout_seconds=0.03))

    async def scenario() -> DependencyDetails:
        started: set[str] = set()
        all_started = asyncio.Event()

        async def probe(name: str) -> None:
            started.add(name)
            if len(started) == 4:
                all_started.set()
            await all_started.wait()
            if name == "redis":
                raise RuntimeError("redis://password@private-host:6379")
            if name == "storage":
                await asyncio.sleep(1)

        for name in ("postgres", "redis", "storage", "milvus"):
            monkeypatch.setattr(checker, name, lambda name=name: probe(name))
        result = await checker.check_detailed()
        assert started == {"postgres", "redis", "storage", "milvus"}
        return result

    result = asyncio.run(scenario())
    assert {name: detail["ready"] for name, detail in result.items()} == {
        "postgres": True,
        "redis": False,
        "storage": False,
        "milvus": True,
    }
    for detail in result.values():
        assert detail["latency_ms"] is not None and detail["latency_ms"] >= 0
        assert datetime.fromisoformat(detail["checked_at"]).tzinfo == UTC
        assert detail["description"]
    assert "password" not in json.dumps(result)
    assert "private-host" not in json.dumps(result)


def test_admin_status_returns_details_without_duplicate_probes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认探测器每个服务只探测一次，公共就绪接口仍只返回原有布尔契约。"""
    calls: dict[str, int] = {}
    checker = client.app.state.health_details_checker.__self__

    async def probe(name: str) -> None:
        calls[name] = calls.get(name, 0) + 1
        if name == "redis":
            raise RuntimeError("private-redis-error")

    for name in ("postgres", "redis", "storage", "milvus", "docling", "worker"):
        monkeypatch.setattr(checker, name, lambda name=name: probe(name))
    identity = login(client)
    assert (
        client.post(
            "/api/v1/auth/development-mode",
            headers={**ORIGIN, "X-CSRF-Token": identity["csrf_token"]},
        ).status_code
        == 200
    )
    response = client.get("/api/v1/admin/development/status")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["checks"] == {
        "postgres": True,
        "redis": False,
        "storage": True,
        "milvus": True,
        "docling": True,
        "worker": True,
    }
    assert calls == {name: 1 for name in body["checks"]}
    assert body["checked_at"]
    assert set(body["check_details"]) == set(body["checks"])
    for name, detail in body["check_details"].items():
        assert detail["ready"] is body["checks"][name]
        assert detail["latency_ms"] is not None
    ready = client.get("/health/ready")
    assert ready.status_code == 503
    core = {name: body["checks"][name] for name in ("postgres", "redis", "storage", "milvus")}
    assert ready.json() == {"status": "unavailable", "checks": core}
    assert calls == {name: 2 if name in core else 1 for name in body["checks"]}
    assert "private-redis-error" not in response.text


@pytest.mark.parametrize("admin,development", [(True, True), (False, True), (True, False)])
def test_custom_checker_is_preserved_and_guarded(admin: bool, development: bool) -> None:
    """注入器只提供布尔值时耗时明确未知，未授权请求不执行探测。"""
    calls = 0

    async def custom() -> dict[str, bool]:
        nonlocal calls
        calls += 1
        return {"postgres": True, "redis": False}

    app = create_app(
        Settings(app_env="test", otel_exporter_otlp_endpoint=""),
        checker=custom,
        engine=create_engine("sqlite://"),
        limiter=lambda *_: None,
    )
    principal = Principal(
        User(platform_admin=admin), LoginSession(development_mode=development), "test-token"
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/admin/development/status").status_code == 401
        app.dependency_overrides[authenticated] = lambda: principal
        response = client.get("/api/v1/admin/development/status")
        if not admin or not development:
            assert response.status_code == 403
            assert calls == 0
            return
        assert response.status_code == 200
        assert calls == 1
        body = response.json()
        assert body["checks"] == {"postgres": True, "redis": False}
        assert all(detail["latency_ms"] is None for detail in body["check_details"].values())
        assert all(
            "自定义探测器" in detail["description"] for detail in body["check_details"].values()
        )
        assert client.get("/health/ready").json()["checks"] == body["checks"]
        assert calls == 2


@pytest.mark.parametrize(
    "embedding_base,docling_code,embedding_valid",
    [
        ("http://embedding:8000/v1", 200, True),
        ("http://127.0.0.1:18081/v1", 503, False),
        ("https://provider.example.test/v1", 200, True),
    ],
)
def test_admin_extra_http_probes_do_not_run_inference_or_change_readiness(
    monkeypatch: pytest.MonkeyPatch,
    embedding_base: str,
    docling_code: int,
    embedding_valid: bool,
) -> None:
    """仅对已知本地 BGE 探测模型状态，外部兼容服务不推测健康路径。"""
    checker = DependencyChecker(
        Settings(
            _env_file=None,
            app_env="test",
            embedding_provider="openai_compatible",
            embedding_base_url=embedding_base,
            docling_url="http://docling:5001",
        )
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "docling":
            return httpx.Response(docling_code)
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "model": checker.settings.embedding_model,
                "dimension": checker.settings.embedding_dimension if embedding_valid else 3,
            },
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(
        health_checks.httpx,
        "AsyncClient",
        lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(respond)),
    )

    async def healthy() -> None:
        pass

    for name in ("postgres", "redis", "storage", "milvus", "worker"):
        monkeypatch.setattr(checker, name, healthy)
    details = asyncio.run(checker.check_admin_detailed())
    assert details["docling"]["ready"] is (docling_code == 200)
    local_embedding = embedding_base.startswith("http://")
    assert ("embedding" in details) is local_embedding
    if local_embedding:
        assert details["embedding"]["ready"] is embedding_valid
    assert all(request.method == "GET" and request.url.path == "/health" for request in requests)
    assert all("authorization" not in request.headers for request in requests)
    assert len(requests) == (2 if local_embedding else 1)
    assert asyncio.run(checker.check()) == {
        "postgres": True,
        "redis": True,
        "storage": True,
        "milvus": True,
    }
    assert len(requests) == (2 if local_embedding else 1)


@pytest.mark.parametrize(
    "replies,expected",
    [
        ({"yugitalk@worker": {"ok": "pong"}}, True),
        ({"other-app@worker": {"ok": "pong"}}, False),
        (None, False),
        ({}, False),
        ([], False),
        ({"worker": {"ok": "unexpected"}}, False),
    ],
)
def test_worker_control_ping_is_bounded_and_requires_a_reply(
    monkeypatch: pytest.MonkeyPatch,
    replies: object,
    expected: bool,
) -> None:
    """工作进程控制通道探测不发布业务任务、禁用重试，空响应不能当作健康。"""
    checker = DependencyChecker(Settings(app_env="test", dependency_timeout_seconds=2))
    app = MagicMock()
    app.__enter__.return_value = app
    app.control.inspect.return_value.ping.return_value = replies
    celery = MagicMock(return_value=app)
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection_type = MagicMock(return_value=connection)
    monkeypatch.setattr(health_checks, "Celery", celery)
    monkeypatch.setattr(health_checks, "_ProbeConnection", connection_type)
    assert asyncio.run(checker._safe_check(checker.worker)) is expected
    assert app.control.mailbox.producer_pool is None
    assert connection_type.call_args.kwargs["connect_timeout"] == 0.5
    assert connection_type.call_args.kwargs["transport_options"] == {
        "socket_timeout": 0.5,
        "socket_connect_timeout": 0.5,
        "retry_on_timeout": False,
        "max_retries": 0,
    }
    connection.ensure_connection.assert_called_once_with(max_retries=0)
    app.control.inspect.assert_called_once_with(
        connection=connection,
        timeout=1,
        limit=1,
        pattern="yugitalk@*",
        matcher="glob",
    )
    app.send_task.assert_not_called()


def test_worker_timeout_does_not_wait_on_the_api_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外层超时可先返回失败；后台线程最终释放连接，不拖住事件循环。"""
    checker = DependencyChecker(Settings(app_env="test", dependency_timeout_seconds=0.03))
    started = threading.Event()
    release = threading.Event()

    def blocking_control() -> None:
        started.set()
        release.wait(timeout=1)

    monkeypatch.setattr(checker, "_worker_ping", blocking_control)

    async def scenario() -> None:
        try:
            assert await checker._safe_check(checker.worker) is False
            assert started.is_set()
            assert not release.is_set()
        finally:
            release.set()

    asyncio.run(scenario())


def test_worker_probe_remains_single_flight_after_timeout_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DNS 等线程工作未结束时，重复轮询不增加真实控制请求；结束后恢复探测。"""
    checker = DependencyChecker(Settings(app_env="test", dependency_timeout_seconds=0.03))
    release = threading.Event()
    calls = 0

    def blocking_control() -> None:
        nonlocal calls
        calls += 1
        release.wait(timeout=1)

    monkeypatch.setattr(checker, "_raw_worker_ping", blocking_control)

    async def scenario() -> None:
        try:
            assert await checker._safe_check(checker.worker) is False
            assert calls == 1
            assert checker._worker_probe_lock.locked()
            assert await checker._safe_check(checker.worker) is False
            assert calls == 1
        finally:
            release.set()

        async def wait_for_thread() -> None:
            while checker._worker_probe_lock.locked():
                await asyncio.sleep(0.001)

        await asyncio.wait_for(wait_for_thread(), timeout=1)
        assert await checker._safe_check(checker.worker) is True
        assert calls == 2

    asyncio.run(scenario())

"""验证实时面板权限、统计口径、窗口过期和敏感数据边界。"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from test_agents import setup_task
from test_identity import login

from app.core.entities import now
from app.observability.dashboard import LiveRequests
from app.tasks.models import Run

pytest_plugins = ["test_identity"]


def test_dashboard_requires_platform_admin(client: TestClient) -> None:
    """匿名和普通用户不能查看跨账号运维聚合，管理员无需开发模式。"""
    path = "/api/v1/admin/monitoring"
    assert client.get(path).status_code == 401
    login(client, "member@example.test")
    assert client.get(path).status_code == 403
    login(client)
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert len(response.json()["http"]["trend"]) == 15
    assert len(response.json()["tasks"]["trend"]) == 15


def test_dashboard_polling_does_not_inflate_business_traffic(client: TestClient) -> None:
    """面板请求不制造业务流量，未知路径和查询参数不进入面板。"""
    login(client)
    client.get("/private-resource", params={"secret": "private-query"})
    first = client.get("/api/v1/admin/monitoring").json()
    second = client.get("/api/v1/admin/monitoring").json()
    assert first["http"]["requests"] == second["http"]["requests"]
    assert second["http"]["client_errors"] >= 1
    assert "private-resource" not in str(second)
    assert "private-query" not in str(second)
    assert any(item["route"] == "unmatched" for item in second["http"]["errors"])


def test_dashboard_reports_settled_tokens_and_unknown_usage(client: TestClient) -> None:
    """累计量与十五分钟量分开统计，未提供完整用量的任务不能伪装为已知用量。"""
    client.app.state.dispatch_run = lambda *_: None
    _, _, task, _ = setup_task(client, "private-prompt")
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        assert run
        run.status = "succeeded"
        run.charged_tokens, run.input_tokens, run.output_tokens = 150, 100, 50
        run.finished_at = now()
        db.commit()
    _, _, task, _ = setup_task(client, "another-private-prompt")
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        assert run
        run.status, run.charged_tokens = "failed", 90
        run.error_code = "model_timeout"
        run.finished_at = now() - timedelta(minutes=20)
        db.commit()
    response = client.get("/api/v1/admin/monitoring")
    tasks = response.json()["tasks"]
    assert tasks["charged_tokens"] == 240
    assert tasks["recent_tokens"] == 150
    assert tasks["input_tokens"] == 100 and tasks["output_tokens"] == 50
    assert tasks["unknown_usage_runs"] == 1
    assert tasks["states"]["failed"] == 1
    assert sum(row["tokens"] for row in tasks["trend"]) == 150
    assert tasks["models"][0]["tokens"] == 240
    assert not tasks["errors"]
    assert "private-prompt" not in response.text


def test_request_window_expires_and_error_buffer_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """历史桶按分钟过期，最近异常数量始终有界，平均和最大耗时正确。"""
    stamp = 1_800_000_000.0
    monkeypatch.setattr("app.observability.dashboard.time.time", lambda: stamp)
    requests = LiveRequests()
    for _ in range(25):
        requests.record("GET", "/health/ready", 503, 12, "request", "trace", "unavailable")
    requests.record("GET", "/health/ready", 200, 38, "request", "trace", None)
    snapshot = requests.snapshot(datetime.fromtimestamp(stamp, UTC))
    assert snapshot["requests"] == 26
    assert snapshot["server_errors"] == 25
    assert len(snapshot["errors"]) == 20
    assert snapshot["routes"][0]["max_ms"] == 38
    assert snapshot["average_ms"] == 13
    later = requests.snapshot(datetime.fromtimestamp(stamp + 15 * 60, UTC))
    assert later["requests"] == 0 and not later["errors"]

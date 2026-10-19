"""验证运维认证、指标标签边界与追踪中的敏感数据保护。"""

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.entities import Base
from app.main import create_app
from app.observability.telemetry import Telemetry


def test_model_and_tool_spans_share_task_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型与工具步骤继承任务标识，追踪中不能采集提示词或工具参数。"""
    from app.agents.contracts import AgentConfig
    from app.agents.graph import run_graph
    from app.observability import telemetry as module
    from app.providers.client import MockProvider

    settings = Settings(_env_file=None, app_env="test", otel_sample_ratio=1)
    telemetry = Telemetry(settings, "test")
    exporter = InMemorySpanExporter()
    telemetry.provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(module, "worker_telemetry", lambda: telemetry)
    with telemetry.span("agent.execute"):
        run_graph(
            settings,
            AgentConfig(name="测试"),
            MockProvider(),
            [
                {"role": "system", "content": "私有上下文"},
                {"role": "user", "content": "请统计保密文本"},
            ],
            lambda *_: None,
            lambda _: None,
            0,
        )
    spans = exporter.get_finished_spans()
    assert {span.name for span in spans} == {"agent.execute", "model.generate", "tool.text_stats"}
    assert len({span.context.trace_id for span in spans}) == 1
    assert all(not span.events and not span.attributes for span in spans)
    telemetry.shutdown()


def test_trace_does_not_capture_exception_content() -> None:
    """异常中的密钥与正文不能被自动写入追踪事件或状态说明。"""
    telemetry = Telemetry(Settings(_env_file=None, otel_sample_ratio=1), "test")
    exporter = InMemorySpanExporter()
    telemetry.provider.add_span_processor(SimpleSpanProcessor(exporter))
    with pytest.raises(ValueError), telemetry.span("test.operation"):
        raise ValueError("private-prompt-and-api-key")
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert not spans[0].events
    assert spans[0].attributes == {"error.type": "ValueError"}
    assert "private-prompt" not in str(spans[0].status)
    telemetry.shutdown()


def test_metrics_require_independent_authentication() -> None:
    """指标必须使用独立令牌；匿名和畸形认证请求都被拒绝。"""
    config = Settings(_env_file=None, app_env="test", metrics_token="m" * 40)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with TestClient(create_app(config, engine=engine)) as client:
        assert client.get("/internal/metrics").status_code == 401
        assert (
            client.get("/internal/metrics", headers={"Authorization": "Bearer wrong"}).status_code
            == 401
        )
        client.get("/unknown-user-id", params={"secret": "private-secret"})
        response = client.get("/internal/metrics", headers={"Authorization": "Bearer " + "m" * 40})
        assert response.status_code == 200
        assert "yugitalk_http_requests_total" in response.text
        assert "private-secret" not in response.text
        assert "unknown-user-id" not in response.text
        assert 'route="unmatched"' in response.text


def test_telemetry_registries_do_not_share_counters() -> None:
    """多个应用实例不会重复注册或共享测试计数，避免误报监控数据。"""
    first = Telemetry(Settings(_env_file=None), "test")
    second = Telemetry(Settings(_env_file=None), "test")
    first.requests.labels("GET", "/health/live", "200").inc()
    assert (
        b'yugitalk_http_requests_total{method="GET",route="/health/live",status="200"} 1.0'
        in first.render()
    )
    assert b"yugitalk_http_requests_total{" not in second.render()
    first.shutdown()
    second.shutdown()


def test_unhandled_failure_is_counted_and_correlated() -> None:
    """意外异常计入五百响应指标和错误追踪，仍向客户端提供关联标识。"""
    from opentelemetry.trace import StatusCode

    app = create_app(Settings(_env_file=None, app_env="test", otel_sample_ratio=1))
    exporter = InMemorySpanExporter()
    app.state.telemetry.provider.add_span_processor(SimpleSpanProcessor(exporter))

    @app.get("/diagnostic-failure")
    def fail() -> None:
        """模拟含有敏感详情的未处理异常。"""
        raise ValueError("private-prompt-and-api-key")

    with TestClient(app) as client:
        response = client.get("/diagnostic-failure")
        assert response.status_code == 500
        assert response.json()["code"] == "internal_error"
        assert response.json()["request_id"] == response.headers["X-Request-ID"]
        assert len(response.headers["X-Trace-ID"]) == 32
        assert "private-prompt" not in response.text
        assert (
            b'yugitalk_http_requests_total{method="GET",'
            b'route="/diagnostic-failure",status="500"} 1.0' in app.state.telemetry.render()
        )
        span = exporter.get_finished_spans()[0]
        assert span.status.status_code == StatusCode.ERROR
        assert span.attributes["error.type"] == "ValueError"
        assert not span.events


def test_failure_logs_only_safe_code_locations() -> None:
    """结构化报错保留错误码和代码位置，不记录异常消息与源码行。"""
    import json
    import logging

    from app.core.logging import JsonFormatter, log_failure

    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        """保留日志对象以验证最终格式化结果。"""

        def emit(self, record: logging.LogRecord) -> None:
            """将安全日志记录交给测试断言。"""
            records.append(record)

    logger = logging.Logger("diagnostic-test")
    logger.addHandler(Capture())
    try:
        raise ValueError("private-key")
    except ValueError as error:
        log_failure(logger, "test.operation", error, error_code="internal_error")
    output = JsonFormatter().format(records[0])
    payload = json.loads(output)
    assert payload["error_type"] == "ValueError"
    assert payload["error_code"] == "internal_error"
    assert payload["frames"]
    assert "private-key" not in output

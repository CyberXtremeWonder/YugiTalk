"""真实协议解析到 Generation 属性的离线回归，不发送业务数据。"""

import json

import httpx
import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app.core.config import Settings
from app.observability import telemetry as module
from app.observability.telemetry import Telemetry
from app.observability.traces import trace_step
from app.providers.client import HTTPProvider, ProviderFailure, Reply


@pytest.fixture
def capture(monkeypatch):
    """复用与 Worker 相同的上下文入口，完全关闭网络导出。"""
    telemetry = Telemetry(Settings(_env_file=None, otel_sample_ratio=1), "test")
    exporter = InMemorySpanExporter()
    telemetry.provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(module, "worker_telemetry", lambda: telemetry)
    yield telemetry, exporter
    telemetry.shutdown()


@pytest.mark.parametrize("protocol", ["openai_compatible", "anthropic"])
def test_stream_retry_records_each_request_once(capture, monkeypatch, protocol):
    """失败重试无伪造用量，完整流只在最终 Reply 后写入真实用量。"""
    from test_provider_stream_limits import fragmented

    monkeypatch.setattr("app.providers.client.time.sleep", lambda _: None)
    attempts = []

    def respond(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(503)
        return httpx.Response(200, text="\n\n".join(fragmented(protocol)))

    settings = Settings(
        _env_file=None, app_env="test", model_base_url="http://model.test/v1",
        model_api_key="private-key", model_max_retries=1,
    )
    provider = HTTPProvider(settings, protocol, "test-model", httpx.MockTransport(respond))
    telemetry, exporter = capture
    with telemetry.span("agent.execute"):
        events = list(provider.stream([
            {"role": "system", "content": "private-system"},
            {"role": "user", "content": "private-question"},
        ], [], 64))
    assert events[-1].reply.input_tokens == 10
    spans = [s for s in exporter.get_finished_spans() if s.name == "model.request"]
    assert len(spans) == 2
    failed, succeeded = spans
    assert failed.status.status_code == StatusCode.ERROR
    assert failed.attributes["langfuse.observation.status_message"] == "model_transient"
    assert "langfuse.observation.usage_details" not in failed.attributes
    assert "langfuse.observation.metadata.first_text_ms" not in failed.attributes
    assert succeeded.attributes["langfuse.observation.metadata.first_text_ms"] >= 0
    assert succeeded.attributes["langfuse.observation.type"] == "generation"
    assert succeeded.attributes["langfuse.observation.model.name"] == "test-model"
    assert succeeded.attributes["langfuse.observation.metadata.retry"] == 1
    assert json.loads(succeeded.attributes["langfuse.observation.usage_details"]) == {
        "input": 10, "output": 8,
    }
    assert succeeded.attributes["langfuse.observation.metadata.usage_complete"] is True
    for span in spans:
        assert not span.events
        assert "private-" not in str(span.attributes)
        assert "cost_details" not in str(span.attributes)


def test_partial_usage_does_not_turn_unknown_into_zero(capture):
    """结构化调用也复用 llm 入口，缺失输出用量只保留已知输入。"""
    telemetry, exporter = capture
    with telemetry.span("rag.execute"):
        with trace_step("llm", "grader", provider="openai_compatible", purpose="grade") as step:
            step.usage(Reply(input_tokens=12))
    span = exporter.get_finished_spans()[0]
    assert json.loads(span.attributes["langfuse.observation.usage_details"]) == {"input": 12}
    assert span.attributes["langfuse.observation.metadata.usage_complete"] is False


def test_incomplete_stream_is_error_without_usage(capture):
    """正文已到达但流未结束时不标记完成，也不伪造最终 Token。"""
    settings = Settings(
        _env_file=None, app_env="test", model_base_url="http://model.test/v1",
        model_api_key="private-key", model_max_retries=0,
    )
    provider = HTTPProvider(settings, "openai_compatible", "test-model", httpx.MockTransport(
        lambda _: httpx.Response(200, text='data: {"choices":[{"delta":{"content":"private"}}]}')
    ))
    telemetry, exporter = capture
    with telemetry.span("agent.execute"):
        with pytest.raises(ProviderFailure, match="model_incomplete"):
            list(provider.stream([{"role": "user", "content": "private"}], [], 64))
    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["langfuse.observation.metadata.outcome"] == "failed"
    assert span.attributes["langfuse.observation.status_message"] == "model_incomplete"
    assert span.attributes["langfuse.observation.metadata.first_text_ms"] >= 0
    assert "langfuse.observation.usage_details" not in span.attributes


def test_closed_generator_is_interrupted(capture):
    """调用方提前关闭流，不把未完成请求显示为正常结束。"""
    telemetry, exporter = capture

    def stream():
        with trace_step("llm", "test-model", provider="openai_compatible"):
            yield "private"

    with telemetry.span("agent.execute"):
        iterator = stream()
        next(iterator)
        iterator.close()
    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["langfuse.observation.metadata.outcome"] == "interrupted"

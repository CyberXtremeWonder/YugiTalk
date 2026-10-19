"""有界指标与手工追踪，禁止采集提示词、响应正文及原始异常。"""

import logging
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from functools import lru_cache

import requests
from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from app.core.config import Settings, load_settings
from app.core.logging import log_failure


class Telemetry:
    """每个进程持有独立指标注册表与追踪提供者，采样和导出队列均有上限。"""

    def __init__(self, settings: Settings, service: str) -> None:
        """初始化元数据指标；只有显式配置 OTLP 地址时才建立网络导出器。"""
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "yugitalk_http_requests",
            "按路由统计的请求数",
            ["method", "route", "status"],
            registry=self.registry,
        )
        self.duration = Histogram(
            "yugitalk_http_response_seconds",
            "响应头生成耗时，流式接口不代表全文耗时",
            ["method", "route"],
            registry=self.registry,
        )
        self.runs = Gauge("yugitalk_runs", "各状态的持久任务数", ["status"], registry=self.registry)
        self.tokens = Gauge(
            "yugitalk_accounted_tokens", "累计结算的保守额度", registry=self.registry
        )
        self.provider = TracerProvider(
            resource=Resource.create({"service.name": "yugitalk-" + service}),
            sampler=ParentBased(TraceIdRatioBased(settings.otel_sample_ratio)),
        )
        if settings.otel_exporter_otlp_endpoint:
            session = requests.Session()
            session.trust_env = False
            exporter = OTLPSpanExporter(
                endpoint=settings.otel_exporter_otlp_endpoint.rstrip("/") + "/v1/traces",
                timeout=2,
                session=session,
            )
            self.provider.add_span_processor(
                BatchSpanProcessor(
                    exporter,
                    max_queue_size=2048,
                    max_export_batch_size=512,
                    schedule_delay_millis=1000,
                )
            )
        self.tracer = self.provider.get_tracer("yugitalk")

    @contextmanager
    def span(self, name: str, parent: Context | None = None) -> Iterator[trace.Span]:
        """异常仅记录类型，禁止 SDK 自动记录可能含敏感参数的异常消息。"""
        with self.tracer.start_as_current_span(
            name,
            context=parent,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            started = time.monotonic()
            logger = logging.getLogger(__name__)
            failed = False
            try:
                yield span
            except Exception as error:
                failed = True
                span.set_attribute("error.type", type(error).__name__)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                log_failure(logger, name, error, trace_id=current_trace_id())
                raise
            finally:
                logger.info(
                    "操作结束",
                    extra={
                        "operation": name,
                        "trace_id": current_trace_id(),
                        "outcome": "raised" if failed else "returned",
                        "duration_ms": round((time.monotonic() - started) * 1000, 2),
                    },
                )

    def render(self) -> bytes:
        """导出 Prometheus 文本，指标标签中没有用户、提示词或原始 URL。"""
        return generate_latest(self.registry)

    def shutdown(self) -> None:
        """进程关闭时完成有界批次导出并释放追踪资源。"""
        self.provider.shutdown()


@lru_cache(maxsize=1)
def worker_telemetry() -> Telemetry:
    """Worker 子进程首次执行时创建追踪器，避免在父进程建立导出线程。"""
    return Telemetry(load_settings(), "worker")


def trace_headers() -> dict[str, str]:
    """仅向内部队列传播服务端生成的追踪标识，不传播用户正文和 baggage。"""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return {key: value for key, value in carrier.items() if key == "traceparent"}


def current_trace_id() -> str:
    """返回当前有效追踪标识，用于结构化日志和客户端请求关联。"""
    context = trace.get_current_span().get_span_context()
    return f"{context.trace_id:032x}" if context.is_valid else ""


def operation_span(name: str) -> AbstractContextManager[trace.Span | None]:
    """仅在已有任务追踪中创建子步骤，离线测试不初始化导出器或读取生产凭证。"""
    if not trace.get_current_span().get_span_context().is_valid:
        return nullcontext(None)
    return worker_telemetry().span(name)

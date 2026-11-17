"""发送合成的学习追踪，不调用模型、不读取业务数据库或用户数据。"""

from app.core.config import load_settings
from app.observability.telemetry import Telemetry, current_trace_id


def main() -> None:
    """完整采样一条父子追踪；是否入库需要在 Langfuse 页面核对。"""
    settings = load_settings()
    if not settings.otel_exporter_otlp_endpoint:
        raise SystemExit("请先配置 OTEL_EXPORTER_OTLP_ENDPOINT 和认证头")
    telemetry = Telemetry(settings.model_copy(update={"otel_sample_ratio": 1.0}), "learning")
    try:
        with telemetry.span("learning.langfuse-smoke") as root:
            root.set_attribute("langfuse.trace.metadata.synthetic", True)
            trace_id = current_trace_id()
            with telemetry.span("learning.child-step"):
                pass
        telemetry.provider.force_flush(timeout_millis=5000)
        print(f"已尝试导出合成追踪，请在 Langfuse 核对 trace_id={trace_id}")
    finally:
        telemetry.shutdown()


if __name__ == "__main__":
    main()

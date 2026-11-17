"""验证本地 Langfuse 接入所需的 OTLP 认证配置。"""

from unittest.mock import patch

from app.core.config import Settings
from app.observability.telemetry import Telemetry


def test_otlp_exporter_receives_private_headers() -> None:
    """来自 Settings 的认证头必须解密传入导出器，配置展示仍隐藏凭证。"""
    settings = Settings(
        _env_file=None,
        otel_exporter_otlp_endpoint="http://localhost:3000/api/public/otel/",
        yugitalk_otlp_headers={
            "Authorization": "Basic private-test-token",
            "x-langfuse-ingestion-version": "4",
        },
    )
    with patch("app.observability.telemetry.OTLPSpanExporter") as exporter:
        telemetry = Telemetry(settings, "test")
        arguments = exporter.call_args.kwargs
        assert arguments["endpoint"] == "http://localhost:3000/api/public/otel/v1/traces"
        assert arguments["headers"] == {
            "Authorization": "Basic private-test-token",
            "x-langfuse-ingestion-version": "4",
        }
        assert arguments["session"].trust_env is False
        assert "private-test-token" not in repr(settings)
        telemetry.shutdown()


def test_no_endpoint_does_not_create_exporter() -> None:
    """默认关闭导出时不会初始化网络导出器。"""
    with patch("app.observability.telemetry.OTLPSpanExporter") as exporter:
        telemetry = Telemetry(Settings(_env_file=None), "test")
        exporter.assert_not_called()
        telemetry.shutdown()

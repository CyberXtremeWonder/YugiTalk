"""使用轻量模型替身验证独立嵌入服务日志与异常协议。"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


def test_embedding_failure_is_safe_and_correlated(monkeypatch: pytest.MonkeyPatch) -> None:
    """推理失败返回安全错误码和关联标识，不泄露输入或异常正文。"""
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=object)
    )
    path = Path(__file__).resolve().parents[1] / "app/rag/local_embedding.py"
    spec = importlib.util.spec_from_file_location("embedding_monitoring_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    @module.app.get("/diagnostic-failure")
    def fail() -> None:
        """模拟推理服务内部失败。"""
        raise ValueError("private-text-and-key")

    # 不启动真实模型生命周期；这里只验证独立服务的 HTTP 监控入口。
    client = TestClient(module.app)
    response = client.get("/diagnostic-failure?text=private-input")
    assert response.status_code == 500
    assert response.json()["code"] == "embedding_service_failed"
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert "private" not in response.text
    denied = client.post("/v1/embeddings", json={"input": ["private-input"]})
    assert denied.status_code == 401
    assert denied.headers["X-Request-ID"]

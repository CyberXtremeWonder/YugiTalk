"""任务连接池复用时仍保持请求认证、向量校验与明确的资源所有权。"""

import httpx
import pytest

from app.core.config import Settings
from app.rag.embeddings import Embeddings, VectorIndex, VectorStoreError


def test_shared_embedding_client_survives_invalid_response_and_preserves_order():
    """坏响应不能关闭调用方连接池；后续批次仍恢复正确向量顺序。"""
    settings = Settings(_env_file=None, app_env="test", embedding_dimension=64)
    requests = []

    def respond(request):
        """第一批缺项，第二批乱序返回两个不同方向的向量。"""
        requests.append(request)
        rows = [] if len(requests) == 1 else [
            {"index": 1, "embedding": [0, 1] + [0] * 62},
            {"index": 0, "embedding": [1, 0] + [0] * 62},
        ]
        return httpx.Response(200, json={"data": rows})

    with httpx.Client(transport=httpx.MockTransport(respond), trust_env=False) as client:
        model = Embeddings(settings, client=client)
        with pytest.raises(ValueError, match="embedding_response_invalid"):
            model.encode(["first", "second"])
        assert not client.is_closed
        assert model.encode(["first", "second"]) == [
            [1, 0] + [0] * 62, [0, 1] + [0] * 62,
        ]
        assert all(request.headers["Authorization"] == "Bearer dev-embedding-key"
                   for request in requests)
    assert client.is_closed


def test_shared_vector_client_checks_business_errors_without_closing_pool():
    """HTTP 200 中的业务失败继续上报，连接池可供后续重试使用。"""
    calls = 0

    def respond(request):
        """业务恢复前后保持相同连接所有权。"""
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"code": 106 if calls == 1 else 0, "data": {}})

    with httpx.Client(transport=httpx.MockTransport(respond), trust_env=False) as client:
        store = VectorIndex(Settings(_env_file=None, app_env="test"), client=client)
        with pytest.raises(VectorStoreError) as failure:
            store.request("collections/has", {"collectionName": "test"})
        assert failure.value.code == 106 and not client.is_closed
        assert store.request("collections/has", {"collectionName": "test"})["code"] == 0
    assert client.is_closed

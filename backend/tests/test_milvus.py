"""验证 Milvus 的业务错误、认证边界与直连行为。"""

import asyncio
from unittest.mock import patch

import httpx
import pytest
from app.core.config import Settings
from app.health.milvus import MilvusClient


@pytest.mark.parametrize(
    "body", [{"code": 1}, [], {"message": "invalid"}, {"code": False}, {"code": 0, "data": {}}]
)
def test_milvus_business_error(body: object) -> None:
    """验证向量库业务错误不会被当作成功响应。"""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    original = httpx.AsyncClient
    with patch(
        "app.health.milvus.httpx.AsyncClient",
        side_effect=lambda **kwargs: original(transport=transport, **kwargs),
    ):
        with pytest.raises(RuntimeError, match="Milvus 请求失败"):
            asyncio.run(MilvusClient(Settings(app_env="test")).ready())


def test_milvus_uses_token_and_configured_database() -> None:
    """验证向量库请求使用配置的认证令牌和数据库。"""

    def handle(request: httpx.Request) -> httpx.Response:
        """提供测试服务响应，验证请求认证和业务错误处理。"""
        assert request.headers["Authorization"] == "Bearer test-token"
        assert b'"dbName":"test_database"' in request.content
        return httpx.Response(200, json={"code": 0, "data": []})

    original = httpx.AsyncClient
    with patch(
        "app.health.milvus.httpx.AsyncClient",
        side_effect=lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    ) as factory:
        asyncio.run(
            MilvusClient(
                Settings(app_env="test", milvus_token="test-token", milvus_database="test_database")
            ).ready()
        )
        assert factory.call_args.kwargs["trust_env"] is False

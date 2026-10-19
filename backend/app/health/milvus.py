"""Milvus REST v2 基础连接，业务检索与空间隔离在知识库阶段实现。"""

import httpx

from app.core.config import Settings


class MilvusClient:
    """同时验证 HTTP 与 Milvus 业务状态，不回显内部错误或认证信息。"""

    def __init__(self, settings: Settings) -> None:
        """保存实例所需的依赖与配置，初始化后续执行使用的状态。"""
        self.settings = settings

    async def request(self, operation: str, payload: dict[str, object]) -> dict[str, object]:
        """发送已认证的受控服务请求，并检查业务响应码。"""
        token = self.settings.milvus_token.get_secret_value()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with httpx.AsyncClient(
            base_url=self.settings.milvus_uri.rstrip("/"),
            headers=headers,
            timeout=self.settings.dependency_timeout_seconds,
            trust_env=False,
        ) as client:
            response = await client.post(
                f"/v2/vectordb/{operation}",
                json={"dbName": self.settings.milvus_database, **payload},
            )
            response.raise_for_status()
            result: object = response.json()
        if (
            not isinstance(result, dict)
            or type(result.get("code")) is not int
            or result.get("code") != 0
        ):
            raise RuntimeError("Milvus 请求失败或响应无效")
        return result

    async def ready(self) -> None:
        """执行实际集合列表请求，验证地址、数据库与凭证可用。"""
        result = await self.request("collections/list", {})
        if not isinstance(result.get("data"), list):
            raise RuntimeError("Milvus 请求失败或响应无效")

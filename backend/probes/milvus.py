"""真实 Milvus 向量往返验收，仅创建并清理本次随机诊断集合。"""

import asyncio
import json
from uuid import uuid4

from app.core.config import Settings
from app.health.milvus import MilvusClient


async def verify() -> dict[str, object]:
    """对真实服务执行隔离探测，验证协议兼容性并清理临时资源。"""
    client = MilvusClient(Settings(dependency_timeout_seconds=30))
    name = f"yugitalk_probe_{uuid4().hex}"
    await client.request(
        "collections/create", {"collectionName": name, "dimension": 3, "metricType": "COSINE"}
    )
    try:
        await client.request(
            "entities/insert",
            {
                "collectionName": name,
                "data": [
                    {"id": 1, "vector": [1.0, 0.0, 0.0]},
                    {"id": 2, "vector": [0.0, 1.0, 0.0]},
                ],
            },
        )
        result = await client.request(
            "entities/search",
            {
                "collectionName": name,
                "data": [[1.0, 0.0, 0.0]],
                "annsField": "vector",
                "limit": 1,
                "consistencyLevel": "Strong",
            },
        )
        rows = result.get("data")
        assert isinstance(rows, list) and rows and rows[0]["id"] == 1
        return {
            "status": "passed",
            "checks": ["集合创建", "向量写入", "相似度检索", "临时集合清理"],
        }
    finally:
        await client.request("collections/drop", {"collectionName": name})


if __name__ == "__main__":
    print(json.dumps(asyncio.run(verify()), ensure_ascii=False))

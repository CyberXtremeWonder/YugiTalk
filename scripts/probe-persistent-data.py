"""在 API 容器中只读检查关系数据、原文、向量检索和 Redis 数据。"""

import hashlib
import json

import redis
from app.core.config import Settings
from app.core.database import create_database
from app.rag.embeddings import Embeddings, VectorIndex
from app.rag.storage import ObjectStorage
from sqlalchemy import inspect, text


def snapshot() -> dict[str, object]:
    """采集只读业务指纹，不输出用户原文、账户信息或模型密钥。"""
    settings = Settings()
    engine = create_database(settings)
    storage = ObjectStorage(settings)
    vectors = VectorIndex(settings)
    result: dict[str, object] = {}
    with engine.connect() as connection:
        tables = inspect(engine).get_table_names()
        result["tables"] = {
            name: connection.execute(
                text(f'SELECT count(*) FROM "{name}"')
            ).scalar_one()
            for name in sorted(tables)
        }
        result["migration"] = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        documents = connection.execute(
            text("SELECT object_key, content_hash FROM documents WHERE status='ready'")
        ).all()
        for key, expected in documents:
            assert hashlib.sha256(storage.get(key)).hexdigest() == expected
        result["original_files_verified"] = len(documents)
    collections = vectors.request("collections/list", {})["data"]
    result["collections"] = {
        name: vectors.request("collections/get_stats", {"collectionName": name})["data"]
        for name in sorted(collections)
    }
    embedding = Embeddings(settings)
    query = embedding.encode(["知识库资料的主要内容是什么？"])[0]
    matches = {}
    for name in sorted(collections):
        rows = vectors.request(
            "entities/search",
            {
                "collectionName": name,
                "data": [query],
                "annsField": "vector",
                "limit": 12,
                "outputFields": ["document_id", "index_version"],
                "consistencyLevel": "Strong",
            },
        )["data"]
        # 仅保存命中标识的指纹；浮点分数可能因重载产生细微差异。
        matches[name] = hashlib.sha256(
            json.dumps(sorted(row["id"] for row in rows)).encode()
        ).hexdigest()
    result["vector_search_fingerprints"] = matches
    result["embedding_dimension"] = len(query)
    client = redis.Redis.from_url(settings.redis_url.get_secret_value())
    persistent_keys = sorted(
        hashlib.sha256(key + client.dump(key)).hexdigest()
        for key in client.scan_iter()
        # Worker 重建会更新控制队列绑定；它不是需要逐字一致的业务数据。
        if client.ttl(key) == -1 and not key.startswith(b"_kombu.binding.")
    )
    result["redis_persistent_keys"] = len(persistent_keys)
    result["redis_persistent_fingerprint"] = hashlib.sha256(
        json.dumps(persistent_keys).encode()
    ).hexdigest()
    client.close()
    engine.dispose()
    return result


if __name__ == "__main__":
    print(json.dumps(snapshot(), sort_keys=True))

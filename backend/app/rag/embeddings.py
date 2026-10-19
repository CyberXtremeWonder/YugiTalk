"""嵌入契约与 Milvus 稠密索引；本地 BGE 和服务器使用相同 HTTP 协议。"""

import asyncio
import hashlib
import json
import math
import time
from collections.abc import Callable
from typing import Any

import httpx

from app.core.config import Settings


class Embeddings:
    """统一本地与服务器嵌入协议，校验向量并生成索引配置标识。"""

    def __init__(self, settings: Settings) -> None:
        """保存实例所需的依赖与配置，初始化后续执行使用的状态。"""
        self.settings = settings
        profile = (
            f"{settings.embedding_provider}:{settings.embedding_model}:"
            f"{settings.embedding_revision}:{settings.embedding_dimension}:cosine"
        )
        self.version = hashlib.sha256(profile.encode()).hexdigest()[:24]
        self.legacy_collection = settings.vector_collection_prefix + "_" + self.version
        schema_version = hashlib.sha256(
            (profile + ":native-bm25-jieba-parent-v3").encode()
        ).hexdigest()[:24]
        self.collection = settings.vector_collection_prefix + "_" + schema_version

    def encode(self, texts: list[str]) -> list[list[float]]:
        """一次至多十六个块；维度、数值和返回顺序均在服务端校验。"""
        if not texts or len(texts) > 16 or any(not text or len(text) > 2000 for text in texts):
            raise ValueError("embedding_input_invalid")
        if self.settings.embedding_provider == "mock":
            # 仅供隔离测试的字符散列向量，不作为真实语义模型或评测结论。
            vectors = []
            for text in texts:
                vector = [0.0] * self.settings.embedding_dimension
                for width in (1, 2, 3):
                    for index in range(max(0, len(text) - width + 1)):
                        bucket = int.from_bytes(
                            hashlib.sha256(text[index : index + width].encode()).digest()[:4], "big"
                        ) % len(vector)
                        vector[bucket] += width
                norm = math.sqrt(sum(value * value for value in vector)) or 1
                vectors.append([value / norm for value in vector])
            return vectors
        with httpx.Client(
            timeout=self.settings.embedding_timeout_seconds, trust_env=False
        ) as client:
            response = client.post(
                self.settings.embedding_base_url.rstrip("/") + "/embeddings",
                headers={
                    "Authorization": "Bearer " + self.settings.embedding_api_key.get_secret_value()
                },
                json={"model": self.settings.embedding_model, "input": texts},
            )
            response.raise_for_status()
            rows = sorted(response.json()["data"], key=lambda row: row["index"])
        if [row["index"] for row in rows] != list(range(len(texts))):
            raise ValueError("embedding_response_invalid")
        result = []
        for row in rows:
            vector = row["embedding"]
            if len(vector) != self.settings.embedding_dimension or any(
                type(v) not in (int, float) or not math.isfinite(v) for v in vector
            ):
                raise ValueError("embedding_dimension_mismatch")
            norm = math.sqrt(sum(v * v for v in vector))
            if not norm:
                raise ValueError("embedding_zero_vector")
            result.append([float(v / norm) for v in vector])
        return result

    async def aencode(self, text: str, deadline: float, guard: Callable[[], None]) -> list[float]:
        """在整个 HTTP 请求截止时间内编码查询，与同步导入共用向量校验契约。"""
        guard()
        if self.settings.embedding_provider == "mock":
            return self.encode([text[:2000]])[0]
        data = await bounded_post(
            self.settings.embedding_base_url.rstrip("/") + "/embeddings",
            {"model": self.settings.embedding_model, "input": [text[:2000]]},
            {"Authorization": "Bearer " + self.settings.embedding_api_key.get_secret_value()},
            deadline,
            guard,
        )
        rows = data.get("data")
        if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("index") != 0:
            raise ValueError("embedding_response_invalid")
        vector = rows[0].get("embedding")
        if (
            not isinstance(vector, list)
            or len(vector) != self.settings.embedding_dimension
            or any(type(value) not in (int, float) or not math.isfinite(value) for value in vector)
        ):
            raise ValueError("embedding_dimension_mismatch")
        norm = math.sqrt(sum(value * value for value in vector))
        if not norm:
            raise ValueError("embedding_zero_vector")
        return [float(value / norm) for value in vector]


async def bounded_post(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    deadline: float,
    guard: Callable[[], None],
) -> dict[str, Any]:
    """限制整个请求时间和响应字节，取消或撤权检查每半秒执行一次。"""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("rag_deadline")
    async with asyncio.timeout(remaining):
        async with httpx.AsyncClient(
            timeout=remaining, trust_env=False, follow_redirects=False
        ) as client:

            async def read() -> dict[str, Any]:
                """流式读取有界响应，防止缓慢分块或无限响应绕过限制。"""
                body = bytearray()
                async with client.stream("POST", url, json=payload, headers=headers) as response:
                    response.raise_for_status()
                    async for part in response.aiter_bytes():
                        guard()
                        body.extend(part)
                        if len(body) > 1048576:
                            raise ValueError("rag_response_too_large")
                result = json.loads(body)
                if not isinstance(result, dict):
                    raise ValueError("rag_response_invalid")
                return result

            task = asyncio.create_task(read())
            try:
                while not task.done():
                    guard()
                    await asyncio.wait({task}, timeout=min(0.5, remaining))
                return await task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)


class VectorIndex:
    """封装向量索引建表、写入、检索与资料删除操作。"""

    def __init__(self, settings: Settings) -> None:
        """保存实例所需的依赖与配置，初始化后续执行使用的状态。"""
        self.settings = settings

    def request(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        """发送已认证的受控服务请求，并检查业务响应码。"""
        token = self.settings.milvus_token.get_secret_value()
        with httpx.Client(timeout=30, trust_env=False) as client:
            response = client.post(
                self.settings.milvus_uri.rstrip("/") + "/v2/vectordb/" + operation,
                headers={"Authorization": "Bearer " + token} if token else {},
                json={"dbName": self.settings.milvus_database, **payload},
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
        if not isinstance(data, dict) or type(data.get("code")) is not int or data["code"] != 0:
            raise RuntimeError("vector_store_unavailable")
        return data

    def ensure(self, embeddings: Embeddings) -> None:
        """检查或创建索引集合，并验证现有向量字段维度。"""
        if not self.request("collections/has", {"collectionName": embeddings.collection})["data"][
            "has"
        ]:
            try:
                self.request(
                    "collections/create",
                    {
                        "collectionName": embeddings.collection,
                        "schema": {
                            "autoId": False,
                            "enableDynamicField": True,
                            "fields": [
                                {
                                    "fieldName": "id",
                                    "dataType": "VarChar",
                                    "isPrimary": True,
                                    "elementTypeParams": {"max_length": "36"},
                                },
                                {
                                    "fieldName": "vector",
                                    "dataType": "FloatVector",
                                    "elementTypeParams": {
                                        "dim": str(self.settings.embedding_dimension)
                                    },
                                },
                                {
                                    "fieldName": "text",
                                    "dataType": "VarChar",
                                    "elementTypeParams": {
                                        "max_length": "8192",
                                        "enable_analyzer": "true",
                                        "analyzer_params": json.dumps({"tokenizer": "jieba"}),
                                    },
                                },
                                {"fieldName": "sparse", "dataType": "SparseFloatVector"},
                            ],
                            "functions": [
                                {
                                    "name": "native_bm25",
                                    "type": "BM25",
                                    "inputFieldNames": ["text"],
                                    "outputFieldNames": ["sparse"],
                                }
                            ],
                        },
                        "indexParams": [
                            {
                                "fieldName": "vector",
                                "indexName": "dense_cosine",
                                "metricType": "COSINE",
                                "indexType": "AUTOINDEX",
                            },
                            {
                                "fieldName": "sparse",
                                "indexName": "native_bm25",
                                "metricType": "BM25",
                                "indexType": "SPARSE_INVERTED_INDEX",
                                "params": {"inverted_index_algo": "DAAT_MAXSCORE"},
                            },
                        ],
                        "params": {"consistencyLevel": "Strong"},
                    },
                )
            except RuntimeError:
                # 并发首次建集合允许另一个任务先完成，但不能吞掉真正的建表失败。
                if not self.request("collections/has", {"collectionName": embeddings.collection})[
                    "data"
                ]["has"]:
                    raise
        description = self.request(
            "collections/describe", {"collectionName": embeddings.collection}
        )["data"]
        fields = description.get("fields", [])
        vector = next(
            (field for field in fields if field.get("name", field.get("fieldName")) == "vector"),
            None,
        )
        if vector is None:
            raise RuntimeError("vector_schema_mismatch")
        parameters = vector.get("params", vector.get("typeParams", {}))
        if isinstance(parameters, list):
            parameters = {p["key"]: p["value"] for p in parameters}
        if int(parameters.get("dim", 0)) != self.settings.embedding_dimension:
            raise RuntimeError("vector_schema_mismatch")
        names = {field.get("name", field.get("fieldName")) for field in fields}
        if not {"text", "sparse"}.issubset(names):
            raise RuntimeError("vector_schema_mismatch")
        functions = description.get("functions", [])
        indexes = description.get("indexes", [])
        text = next(
            field for field in fields if field.get("name", field.get("fieldName")) == "text"
        )
        text_params = text.get("params", {})
        if isinstance(text_params, list):
            text_params = {item["key"]: item["value"] for item in text_params}
        if (
            description.get("enableDynamicField") is not True
            or str(text_params.get("enable_analyzer")).lower() != "true"
            or json.loads(text_params.get("analyzer_params", "{}")) != {"tokenizer": "jieba"}
            or not any(
                function.get("type") in (1, "BM25")
                and function.get("inputFieldNames") == ["text"]
                and function.get("outputFieldNames") == ["sparse"]
                for function in functions
            )
            or not any(
                index.get("fieldName") == "sparse" and index.get("metricType") == "BM25"
                for index in indexes
            )
        ):
            raise RuntimeError("vector_schema_mismatch")

    async def recall(
        self,
        collection: str,
        workspace: str,
        base: str,
        documents: dict[str, int],
        data: list[float] | str,
        sparse: bool,
        deadline: float,
        guard: Callable[[], None],
    ) -> list[dict[str, Any]]:
        """在相同授权版本过滤下执行稠密或 Milvus 原生 BM25 召回，不隐藏存储故障。"""
        if not documents:
            return []
        versions = " or ".join(
            f"(document_id == {json.dumps(key)} and index_version == {version})"
            for key, version in documents.items()
        )
        token = self.settings.milvus_token.get_secret_value()
        result = await bounded_post(
            self.settings.milvus_uri.rstrip("/") + "/v2/vectordb/entities/search",
            {
                "dbName": self.settings.milvus_database,
                "collectionName": collection,
                "data": [data],
                "annsField": "sparse" if sparse else "vector",
                "filter": (f"knowledge_base_id == {json.dumps(base)} and ({versions})"),
                "limit": self.settings.rag_candidate_limit,
                "outputFields": ["document_id", "index_version"],
                "consistencyLevel": "Strong",
                "searchParams": {"metricType": "BM25" if sparse else "COSINE", "params": {}},
            },
            {"Authorization": "Bearer " + token} if token else {},
            deadline,
            guard,
        )
        if (
            type(result.get("code")) is not int
            or result["code"] != 0
            or not isinstance(result.get("data"), list)
        ):
            raise RuntimeError("vector_store_unavailable")
        hits: list[dict[str, Any]] = result["data"]
        if len(hits) > self.settings.rag_candidate_limit or any(
            not isinstance(hit, dict)
            or not isinstance(hit.get("id"), str)
            or type(hit.get("distance")) not in (int, float)
            or not math.isfinite(hit["distance"])
            for hit in hits
        ):
            raise ValueError("vector_response_invalid")
        return hits

    def upsert(self, collection: str, rows: list[dict[str, Any]]) -> None:
        """向指定集合批量写入或更新带版本标识的向量条目。"""
        self.request("entities/upsert", {"collectionName": collection, "data": rows})

    def remove(self, collection: str, document_id: str) -> None:
        """按资料标识删除指定集合的条目，避免留下可召回的旧向量。"""
        if self.request("collections/has", {"collectionName": collection})["data"]["has"]:
            self.request(
                "entities/delete",
                {
                    "collectionName": collection,
                    "filter": "document_id == " + json.dumps(document_id),
                },
            )

    def search(
        self,
        collection: str,
        workspace: str,
        base: str,
        documents: dict[str, int],
        vector: list[float],
    ) -> list[dict[str, Any]]:
        # 标量过滤由授权后的 UUID 构建，在 Milvus 召回前完成空间与资料隔离。
        """在空间、知识库和资料版本过滤下执行向量召回。"""
        versions = " or ".join(
            f"(document_id == {json.dumps(key)} and index_version == {version})"
            for key, version in documents.items()
        )
        expression = f"knowledge_base_id == {json.dumps(base)} and ({versions})"
        data = self.request(
            "entities/search",
            {
                "collectionName": collection,
                "data": [vector],
                "annsField": "vector",
                "filter": expression,
                "limit": 12,
                "outputFields": ["document_id", "index_version"],
                "consistencyLevel": "Strong",
            },
        )["data"]
        return data if isinstance(data, list) else []

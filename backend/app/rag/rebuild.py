"""受控迁移旧稠密集合，复用现有向量并在验证后切换 SQL 集合指针。"""

import json
import math
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.rag.embeddings import Embeddings, VectorIndex
from app.rag.hierarchy import attach_parents
from app.rag.models import Chunk, Document


def rebuild_document(engine: Engine, settings: Settings, key: str) -> dict[str, Any]:
    """旧版本仍可检索；失败回滚关系表，候选向量不在发布前进入检索范围。"""
    embedding, store = Embeddings(settings), VectorIndex(settings)
    store.ensure(embedding)
    with Session(engine) as db:
        doc = db.scalar(select(Document).where(Document.id == key).with_for_update())
        if not doc or doc.status != "ready" or doc.embedding_version != embedding.version:
            raise ValueError("document_rebuild_unavailable")
        if doc.collection_name == embedding.collection:
            return {"document_id": key, "status": "already_current"}
        if doc.collection_name != embedding.legacy_collection:
            raise ValueError("vector_schema_mismatch")
        chunks = list(
            db.scalars(select(Chunk).where(Chunk.document_id == key).order_by(Chunk.ordinal))
        )
        if (
            not chunks
            or len(chunks) > 1000
            or any(chunk.index_version != doc.index_version or chunk.parent_id for chunk in chunks)
        ):
            raise ValueError("document_rebuild_manifest_invalid")
        parts: list[dict[str, Any]] = [
            {"ordinal": chunk.ordinal, "page": chunk.page, "content": chunk.content}
            for chunk in chunks
        ]
        attach_parents(db, key, doc.index_version, parts)
        for start in range(0, len(chunks), 16):
            # 模型语义未变，只迁移集合结构；复用原向量并补齐文本字段，无需重新嵌入。
            batch = chunks[start : start + 16]
            result = store.request(
                "entities/query",
                {
                    "collectionName": doc.collection_name,
                    "filter": "id in " + json.dumps([chunk.id for chunk in batch]),
                    "outputFields": ["id", "vector", "document_id", "index_version"],
                    "limit": 16,
                    "consistencyLevel": "Strong",
                },
            )["data"]
            rows = {row["id"]: row for row in result}
            if set(rows) != {chunk.id for chunk in batch}:
                raise ValueError("document_rebuild_manifest_invalid")
            output = []
            for offset, chunk in enumerate(batch, start):
                vector = rows[chunk.id].get("vector")
                if (
                    rows[chunk.id].get("document_id") != doc.id
                    or rows[chunk.id].get("index_version") != doc.index_version
                    or not isinstance(vector, list)
                    or len(vector) != settings.embedding_dimension
                    or any(
                        type(value) not in (int, float) or not math.isfinite(value)
                        for value in vector
                    )
                ):
                    raise ValueError("document_rebuild_manifest_invalid")
                output.append(
                    {
                        "id": chunk.id,
                        "vector": vector,
                        "text": chunk.content,
                        "account_id": doc.account_id,
                        "knowledge_base_id": doc.knowledge_base_id,
                        "document_id": key,
                        "index_version": doc.index_version,
                        "chunk_id": chunk.id,
                        "embedding_version": doc.embedding_version,
                    }
                )
                chunk.parent_id = parts[offset]["parent_id"]
            store.upsert(embedding.collection, output)
        # 在同一行锁下核验候选精确身份，再原子发布；不删除旧集合或原始资料。
        manifest = store.request(
            "entities/query",
            {
                "collectionName": embedding.collection,
                "filter": (
                    f"document_id == {json.dumps(key)} and index_version == {doc.index_version}"
                ),
                "outputFields": ["id"],
                "limit": 1001,
                "consistencyLevel": "Strong",
            },
        )["data"]
        if {row["id"] for row in manifest} != {chunk.id for chunk in chunks} or len(
            manifest
        ) != len(chunks):
            raise ValueError("document_rebuild_manifest_invalid")
        doc.collection_name = embedding.collection
        # SQL 指针切换是发布点；在这之前查询仍使用旧集合，候选写入不会提前暴露。
        doc.chunk_strategy = "leaf700-parent3-native-bm25-jieba-v2"
        db.commit()
        return {
            "document_id": key,
            "status": "rebuilt",
            "chunks": len(chunks),
            "generation": doc.index_version,
        }

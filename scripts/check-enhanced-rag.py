"""仅在阶段六隔离容器内验证真实 PostgreSQL、Milvus、BGE-M3 与受控索引升级。"""

import asyncio
import hashlib
import json
import time
from uuid import uuid4

from app.core.config import Settings
from app.core.database import create_database
from app.core.entities import identifier
from app.identity.models import User
from app.main import create_app
from app.rag.embeddings import Embeddings, VectorIndex
from app.rag.indexing import cleanup_document, index_document
from app.rag.models import Chunk, Document, KnowledgeBase, ParentChunk
from app.rag.pipeline import RagPipeline
from app.rag.rebuild import rebuild_document
from app.rag.retrieval import document_scope, materialize, recall_queries
from app.rag.storage import ObjectStorage
from sqlalchemy import delete, select
from sqlalchemy.orm import Session


def main() -> None:
    """只创建和删除本次随机测试范围，禁止把验收脚本用于正式业务环境。"""
    settings = Settings()
    if (
        settings.model_base_url != "https://model-fixture:8000/v1"
        or not settings.storage_local_test_fixture
        or settings.auth_trusted_origins != ["https://localhost:18443"]
    ):
        raise RuntimeError("只允许阶段六隔离测试环境")
    settings = settings.model_copy(
        update={"model_provider": "mock", "model_id": "mock-v1", "rag_timeout_seconds": 120}
    )
    engine = create_database(settings)
    # 主应用导入全部领域映射；脚本不启动 API，也不派发收费模型任务。
    create_app(settings, engine=engine, limiter=lambda *args: None)
    user, base, doc_id = (identifier() for _ in range(3))
    space = user
    nonce = uuid4().hex[:10]
    text = (
        "Milvus 保存 BGE-M3 的向量，PostgreSQL 保存资料版本，RAG 使用混合检索。" * 120
        + "\n独特测试词 BX942_Z8。"
    )
    content = text.encode()
    object_key = f"{space}/{base}/{doc_id}/test.md"
    embedding, vectors = Embeddings(settings), VectorIndex(settings)
    result = {
        "passed": False,
        "embedding": "BAAI/bge-m3",
        "dimension": settings.embedding_dimension,
        "paid_model_calls": 0,
        "model_mode": "offline_rules",
        "checks": {},
        "cleanup_passed": False,
    }
    started = time.monotonic()
    try:
        with Session(engine) as db:
            db.add(
                User(
                    id=user,
                    email=nonce + "@enhanced-rag.test",
                    name="离线验收",
                    password_hash="unusable-test-identity",
                )
            )
            db.flush()
            db.add(KnowledgeBase(id=base, account_id=space, name="七步真实索引验收"))
            db.flush()
            db.add(
                Document(
                    id=doc_id,
                    account_id=space,
                    knowledge_base_id=base,
                    filename="七步验收.md",
                    size=len(content),
                    content_hash=hashlib.sha256(content).hexdigest(),
                    object_key=object_key,
                    embedding_version=embedding.version,
                    collection_name=embedding.collection,
                )
            )
            db.commit()
        ObjectStorage(settings).put(object_key, content)
        index_document(engine, settings, doc_id, 1)
        with Session(engine) as db:
            doc = db.get(Document, doc_id)
            if not doc or doc.status != "ready":
                raise RuntimeError("真实索引失败：" + str(doc.error_code if doc else "missing"))
            scope = document_scope(db, settings, space, user, base)
            chunks = list(
                db.scalars(select(Chunk).where(Chunk.document_id == doc_id).order_by(Chunk.ordinal))
            )
            child_ids = [chunk.id for chunk in chunks]
        sparse = asyncio.run(
            vectors.recall(
                embedding.collection,
                space,
                base,
                {doc_id: 1},
                "BX942_Z8",
                True,
                time.monotonic() + 15,
                lambda: None,
            )
        )
        assert sparse and sparse[0]["id"] in child_ids
        result["checks"]["native_bm25"] = {
            "hits": len(sparse),
            "positive_score": sparse[0]["distance"] > 0,
        }
        description = vectors.request(
            "collections/describe", {"collectionName": embedding.collection}
        )["data"]
        result["checks"]["schema"] = {
            "collection": embedding.collection,
            "fields": description.get("fields"),
            "functions": description.get("functions"),
        }
        hits, trace = asyncio.run(
            recall_queries(
                settings,
                space,
                base,
                scope,
                ["Milvus 向量", "PostgreSQL 资料版本"],
                time.monotonic() + 30,
                lambda: None,
            )
        )
        with Session(engine) as db:
            sources = materialize(db, settings, space, user, base, scope, hits)
        assert trace["native_bm25"] and sources and any(source.get("merged") for source in sources)
        result["checks"]["parallel_hybrid_and_merge"] = {
            **trace,
            "merged": sum(bool(source.get("merged")) for source in sources),
        }
        state = RagPipeline(
            engine, settings, space, user, base, lambda *args: None, lambda state: None
        ).run("比较 Milvus 和 PostgreSQL 的用途")
        assert state["phase"] == "done" and state["complexity"] == "complex" and state["sources"]
        result["checks"]["seven_step_pipeline"] = {
            "trace": state["trace"],
            "grade_count": state["grade_count"],
            "rewrite_count": state["rewrite_count"],
            "evidence_ids": [source["evidence_id"] for source in state["sources"]],
        }
        # 将本次测试资料转成旧索引形态，再验证不重新编码的受控升级。
        if not vectors.request("collections/has", {"collectionName": embedding.legacy_collection})[
            "data"
        ]["has"]:
            vectors.request(
                "collections/create",
                {
                    "collectionName": embedding.legacy_collection,
                    "dimension": settings.embedding_dimension,
                    "metricType": "COSINE",
                    "idType": "VarChar",
                    "autoID": False,
                    "params": {
                        "max_length": 36,
                        "enableDynamicField": True,
                        "consistencyLevel": "Strong",
                    },
                },
            )
        old_rows = vectors.request(
            "entities/query",
            {
                "collectionName": embedding.collection,
                "filter": "document_id == " + json.dumps(doc_id),
                "outputFields": ["id", "vector", "document_id", "index_version"],
                "limit": 1000,
            },
        )["data"]
        vectors.upsert(
            embedding.legacy_collection,
            [{**row, "account_id": space, "knowledge_base_id": base} for row in old_rows],
        )
        with Session(engine) as db:
            db.get(Document, doc_id).collection_name = embedding.legacy_collection
            for chunk in db.scalars(select(Chunk).where(Chunk.document_id == doc_id)):
                chunk.parent_id = None
            db.flush()
            db.execute(delete(ParentChunk).where(ParentChunk.document_id == doc_id))
            db.commit()
        rebuilt = rebuild_document(engine, settings, doc_id)
        assert rebuilt["status"] == "rebuilt"
        result["checks"]["controlled_rebuild"] = rebuilt
        with Session(engine) as db:
            db.get(Document, doc_id).status = "deleting"
            db.commit()
            assert document_scope(db, settings, space, user, base) == {}
        result["checks"]["deletion_isolation"] = True
        result["passed"] = True
    finally:
        with Session(engine) as db:
            doc = db.get(Document, doc_id)
            if doc:
                doc.status = "deleting"
                db.commit()
        cleanup_document(engine, settings, doc_id)
        vectors.remove(embedding.legacy_collection, doc_id)
        vectors.remove(embedding.collection, doc_id)
        with Session(engine) as db:
            doc = db.get(Document, doc_id)
            if doc and doc.status != "deleted":
                raise RuntimeError("验收资料清理失败")
            db.execute(delete(Document).where(Document.id == doc_id))
            db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == base))
            db.execute(delete(User).where(User.id == user))
            db.commit()
        result["cleanup_passed"] = True
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        engine.dispose()
        print("RESULT:" + json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

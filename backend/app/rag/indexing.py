"""原文、分块、向量的可重试导入与授权检索；跨存储不假设共享事务。"""

import json
import logging
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import Engine, delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.entities import identifier, now
from app.core.logging import log_failure
from app.observability.telemetry import current_trace_id
from app.rag.embeddings import Embeddings, VectorIndex
from app.rag.graph import clear_graph, fuse_graph, queue_graph
from app.rag.graph_models import GraphBuild
from app.rag.hierarchy import attach_parents
from app.rag.models import Chunk, Document, ParentChunk
from app.rag.storage import ObjectStorage
from app.tasks.reliability import LeaseKeeper, utc


def parse_file(content: bytes, suffix: str, strategy: str = "fixed") -> list[dict[str, Any]]:
    """在受限子进程中解析资料，限制执行时间和返回数据大小。"""
    result = subprocess.run(
        [sys.executable, "-m", "app.rag.parser", suffix, strategy],
        cwd=Path(__file__).resolve().parents[2],
        input=content,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode or len(result.stdout) > 3 * 1024 * 1024:
        raise ValueError("document_parse_failed")
    data = json.loads(result.stdout)
    if "error" in data:
        raise ValueError(data["error"])
    chunks: list[dict[str, Any]] = data["chunks"]
    return chunks


def owns_document(doc: Document | None, generation: int, token: str) -> bool:
    """校验资料版本及租约，防止过期工作进程发布结果。"""
    return bool(
        doc
        and doc.index_version == generation
        and doc.lease_token == token
        and doc.lease_until
        and utc(doc.lease_until) > now()
        and doc.status in {"parsing", "indexing"}
    )


def mark_progress(
    engine: Engine, key: str, generation: int, stage: str, token: str, **counts: int
) -> bool:
    """外部调用前提交当前步骤；独立事务让页面能看到正在执行的阶段。"""
    with Session(engine) as db:
        doc = db.scalar(select(Document).where(Document.id == key).with_for_update())
        if not owns_document(doc, generation, token):
            return False
        assert doc
        doc.progress = {**doc.progress, "stage": stage, **counts}
        db.commit()
        return True


def index_document(engine: Engine, settings: Settings, key: str, generation: int) -> None:
    """领取资料索引任务并维持租约，避免多个进程重复发布。"""
    token = identifier()
    with LeaseKeeper(engine, settings, Document, key, token):
        _index_document(engine, settings, key, generation, token)


def _index_document(
    engine: Engine, settings: Settings, key: str, generation: int, token: str
) -> None:
    """依次读取、解析、分批嵌入和写入资料，提交每一步进度。"""
    embeddings, vectors = Embeddings(settings), VectorIndex(settings)
    with Session(engine) as db:
        doc = db.scalar(select(Document).where(Document.id == key).with_for_update())
        if doc is None or doc.index_version != generation or doc.status != "queued":
            return
        if doc.embedding_version != embeddings.version:
            doc.status, doc.error_code = "failed", "embedding_configuration_changed"
            doc.progress = {"stage": "queued", "failed_stage": "queued"}
            db.commit()
            return
        doc.status, doc.error_code = "parsing", None
        doc.lease_token, doc.lease_until = (
            token,
            now() + timedelta(seconds=settings.run_lease_seconds),
        )
        doc.attempts += 1
        doc.progress = {
            "stage": "reading",
            "total_chunks": 0,
            "embedded_chunks": 0,
            "indexed_chunks": 0,
        }
        object_key, filename, workspace, base, collection = (
            doc.object_key,
            doc.filename,
            doc.account_id,
            doc.knowledge_base_id,
            doc.collection_name,
        )
        strategy = (
            "markdown_sections"
            if doc.chunk_strategy == "markdown-sections-700-parent-v1"
            else "fixed"
        )
        db.commit()
    stage = "reading"
    try:
        content = ObjectStorage(settings).get(object_key)
        stage = "parsing"
        if not mark_progress(engine, key, generation, stage, token):
            return
        parts = (
            parse_file(content, Path(filename).suffix.lower(), strategy)
            if strategy != "fixed"
            else parse_file(content, Path(filename).suffix.lower())
        )
        stage = "preparing"
        if not mark_progress(engine, key, generation, stage, token, total_chunks=len(parts)):
            return
        vectors.ensure(embeddings)
        with Session(engine) as db:
            doc = db.scalar(select(Document).where(Document.id == key).with_for_update())
            if not owns_document(doc, generation, token) or doc is None or doc.status != "parsing":
                return
            doc.status = "indexing"
            doc.chunk_strategy = (
                "markdown-sections-700-parent-v1"
                if strategy == "markdown_sections"
                else "leaf700-parent3-native-bm25-jieba-v2"
            )
            clear_graph(db, key)
            db.execute(delete(Chunk).where(Chunk.document_id == key))
            db.execute(delete(ParentChunk).where(ParentChunk.document_id == key))
            attach_parents(db, key, generation, parts)
            db.commit()
        for start in range(0, len(parts), 16):
            batch = parts[start : start + 16]
            stage = "embedding"
            if not mark_progress(
                engine, key, generation, stage, token, batch_number=start // 16 + 1
            ):
                return
            encoded = embeddings.encode([part["content"] for part in batch])
            stage = "writing"
            completed = start + len(batch)
            if not mark_progress(engine, key, generation, stage, token, embedded_chunks=completed):
                return
            with Session(engine) as db:
                doc = db.scalar(select(Document).where(Document.id == key).with_for_update())
                if (
                    not owns_document(doc, generation, token)
                    or doc is None
                    or doc.status != "indexing"
                ):
                    return
                rows = []
                for part, vector in zip(batch, encoded, strict=True):
                    chunk_id = str(uuid5(NAMESPACE_URL, f"{key}:{generation}:{part['ordinal']}"))
                    db.add(
                        Chunk(
                            id=chunk_id,
                            document_id=key,
                            ordinal=part["ordinal"],
                            page=part["page"],
                            content=part["content"],
                            index_version=generation,
                            parent_id=part["parent_id"],
                        )
                    )
                    rows.append(
                        {
                            "id": chunk_id,
                            "vector": vector,
                            "account_id": workspace,
                            "knowledge_base_id": base,
                            "document_id": key,
                            "chunk_id": chunk_id,
                            "index_version": generation,
                            "embedding_version": embeddings.version,
                            "text": part["content"],
                        }
                    )
                # 计数仅在该批向量和 SQL 分块均成功后推进；失败保留最后成功的批次。
                # 持有文档锁写入该批向量，删除提交后旧任务不能再写入。
                vectors.upsert(collection, rows)
                if not owns_document(doc, generation, token):
                    # 外部写入可能跨过租约到期；回滚 SQL 半成品，恢复使用新版本隔离向量。
                    db.rollback()
                    return
                doc.progress = {**doc.progress, "indexed_chunks": completed}
                db.commit()
        with Session(engine) as db:
            doc = db.scalar(select(Document).where(Document.id == key).with_for_update())
            if owns_document(doc, generation, token) and doc:
                doc.status = "ready"
                doc.lease_token, doc.lease_until = None, None
                doc.progress = {**doc.progress, "stage": "ready"}
                if settings.rag_graph_enabled:
                    queue_graph(db, doc)
                db.commit()
    except Exception as error:
        code = import_error(stage, error)
        log_failure(
            logging.getLogger(__name__),
            "knowledge.index",
            error,
            document_id=key,
            stage=stage,
            error_code=code,
            trace_id=current_trace_id(),
        )
        with Session(engine) as db:
            doc = db.scalar(select(Document).where(Document.id == key).with_for_update())
            if owns_document(doc, generation, token) and doc:
                doc.status, doc.error_code = "failed", code
                doc.lease_token, doc.lease_until = None, None
                doc.progress = {**doc.progress, "stage": stage, "failed_stage": stage}
                db.commit()


def import_error(stage: str, error: Exception) -> str:
    """只公开安全的原因码，不把模型密钥、连接地址或外部响应体发给浏览器。"""
    if isinstance(error, SoftTimeLimitExceeded):
        return "document_index_timeout"
    codes = {
        "empty_or_oversized_file",
        "invalid_pdf",
        "pdf_encrypted_or_too_many_pages",
        "pdf_page_too_large",
        "text_too_large",
        "pdf_requires_ocr",
        "invalid_text",
        "unsupported_file_type",
        "empty_document",
        "too_many_chunks",
        "document_parse_failed",
        "embedding_input_invalid",
        "embedding_response_invalid",
        "embedding_dimension_mismatch",
        "embedding_zero_vector",
    }
    if isinstance(error, ValueError) and str(error) in codes:
        return str(error)
    # 仅保留已知安全原因码，避免集合结构错误被通用准备失败掩盖。
    if stage == "preparing":
        if isinstance(error, SQLAlchemyError):
            return "document_chunk_storage_failed"
        if isinstance(error, RuntimeError) and str(error) in {
            "vector_schema_mismatch",
            "vector_store_unavailable",
        }:
            return str(error)
        if isinstance(error, httpx.TimeoutException):
            return "vector_prepare_timeout"
    if stage == "embedding":
        if isinstance(error, httpx.TimeoutException):
            return "embedding_timeout"
        if isinstance(error, httpx.HTTPStatusError):
            if error.response.status_code in {401, 403}:
                return "embedding_auth_failed"
            if error.response.status_code == 429:
                return "embedding_rate_limited"
        return "embedding_service_failed"
    if stage == "parsing" and isinstance(error, subprocess.TimeoutExpired):
        return "document_parse_timeout"
    return {
        "reading": "document_storage_read_failed",
        "parsing": "document_parse_failed",
        "preparing": "vector_prepare_failed",
        "writing": "vector_write_failed",
    }.get(stage, "document_index_failed")


def cleanup_document(engine: Engine, settings: Settings, key: str) -> None:
    """删除已撤销资料对应的对象和索引，保留状态隔离边界。"""
    with Session(engine) as db:
        doc = db.scalar(select(Document).where(Document.id == key).with_for_update())
        if doc is None or doc.status != "deleting":
            return
        try:
            VectorIndex(settings).remove(doc.collection_name, doc.id)
            ObjectStorage(settings).remove(doc.object_key)
            clear_graph(db, key)
            db.execute(delete(GraphBuild).where(GraphBuild.document_id == key))
            db.execute(delete(Chunk).where(Chunk.document_id == key))
            db.execute(delete(ParentChunk).where(ParentChunk.document_id == key))
            doc.status, doc.error_code = "deleted", None
        except Exception as error:
            # 墓碑继续生效；用户可重试清理，不把删除失败文档重新开放检索。
            doc.error_code = "document_cleanup_failed"
            log_failure(
                logging.getLogger(__name__),
                "knowledge.cleanup",
                error,
                error_code="document_cleanup_failed",
                document_id=key,
                trace_id=current_trace_id(),
            )
        db.commit()


def retrieve(
    db: Session, settings: Settings, workspace: str, user: str, base_id: str, query: str
) -> list[dict[str, Any]]:
    """提供知识库检索入口，使用与七步编排相同的混合召回和权威来源校验。"""
    import asyncio
    import time

    from app.rag.retrieval import document_scope, materialize, pack_evidence, recall_queries

    scope = document_scope(db, settings, workspace, user, base_id)
    # 外部请求前释放读事务；返回后重新授权，不长时间占据数据库连接。
    db.rollback()
    hits, _ = asyncio.run(
        recall_queries(
            settings,
            workspace,
            base_id,
            scope,
            [query],
            time.monotonic() + settings.rag_timeout_seconds,
            lambda: None,
            VectorIndex(settings),
        )
    )
    db.expire_all()
    current_scope = document_scope(db, settings, workspace, user, base_id)
    graph_scope = {key: value for key, value in scope.items() if current_scope.get(key) == value}
    hits, _ = fuse_graph(db, settings, graph_scope, query, hits)
    return pack_evidence(
        materialize(db, settings, workspace, user, base_id, scope, hits),
        settings.rag_context_characters,
    )


def dispatch_document(key: str, generation: int, cleanup: bool = False) -> None:
    """将资料索引任务投递到指定队列，持久状态供调度补投。"""
    from app.observability.telemetry import trace_headers
    from app.tasks.celery_app import celery_app

    celery_app.send_task(
        "yugitalk.knowledge.cleanup" if cleanup else "yugitalk.knowledge.index",
        args=[key] if cleanup else [key, generation],
        headers=trace_headers(),
    )

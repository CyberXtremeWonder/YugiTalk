"""多问题并行混合召回、RRF 融合及经 SQL 权威关系校验的父级合并。"""

import asyncio
import hashlib
import math
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.identity.models import User
from app.rag.embeddings import Embeddings, VectorIndex
from app.rag.models import Chunk, Document, KnowledgeBase, ParentChunk


def document_scope(
    db: Session, settings: Settings, workspace: str, user: str, base: str
) -> dict[str, tuple[int, str]]:
    """冻结已授权资料及集合版本，避免并行分支共享数据库会话。"""
    owner = db.get(User, user)
    if workspace != user or owner is None or not owner.active:
        raise ValueError("knowledge_permission_revoked")
    row = db.get(KnowledgeBase, base)
    if row is None or row.deleted or row.account_id != workspace:
        raise ValueError("knowledge_unavailable")
    documents = list(
        db.scalars(
            select(Document)
            .where(
                Document.knowledge_base_id == base,
                Document.account_id == workspace,
                Document.status == "ready",
                Document.embedding_version == Embeddings(settings).version,
            )
            .limit(201)
        )
    )
    if len(documents) > 200:
        raise ValueError("knowledge_document_limit")
    return {doc.id: (doc.index_version, doc.collection_name) for doc in documents}


def rrf(rankings: list[list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    """按排名融合，不相加余弦和 BM25 原始分数；每路重复身份只计一次。"""
    totals: dict[str, float] = defaultdict(float)
    rows: dict[str, dict[str, Any]] = {}
    for ranking in rankings:
        # 各路分数尺度不同，只累加排名的倒数；同一条证据跨路命中可获得更高排名。
        seen: set[str] = set()
        for rank, row in enumerate(ranking, 1):
            key = str(row["id"])
            if key in seen:
                continue
            seen.add(key)
            totals[key] += 1 / (60 + rank)
            rows[key] = row
    ordered = sorted(totals, key=lambda key: (-totals[key], key))[:limit]
    return [{**rows[key], "rrf_score": totals[key]} for key in ordered]


async def recall_queries(
    settings: Settings,
    workspace: str,
    base: str,
    scope: dict[str, tuple[int, str]],
    queries: list[str],
    deadline: float,
    guard: Callable[[], None],
    index: VectorIndex | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """最多四个查询并行执行，异步超时会取消真实请求；旧集合明确记录稠密兼容模式。"""
    embeddings = Embeddings(settings)
    store = index or VectorIndex(settings)
    groups: dict[str, dict[str, int]] = defaultdict(dict)
    for document, (version, collection) in scope.items():
        # 按集合分组，以便同时兼容旧稠密索引与新混合索引，但禁止混入未知结构的集合。
        if collection not in {embeddings.collection, embeddings.legacy_collection}:
            raise ValueError("vector_schema_mismatch")
        groups[collection][document] = version
    semaphore = asyncio.Semaphore(settings.rag_parallel_queries)

    async def branch(query: str) -> list[list[dict[str, Any]]]:
        """每个分支只编码一次，同一向量用于其所有目标集合。"""
        async with semaphore:
            guard()
            if not groups:
                return []
            vector = await embeddings.aencode(query, deadline, guard)
            jobs = []
            kinds = []
            for collection, documents in groups.items():
                jobs.append(
                    store.recall(
                        collection, workspace, base, documents, vector, False, deadline, guard
                    )
                )
                kinds.append(False)
                if collection == embeddings.collection:
                    jobs.append(
                        store.recall(
                            collection,
                            workspace,
                            base,
                            documents,
                            query[:2000],
                            True,
                            deadline,
                            guard,
                        )
                    )
                    kinds.append(True)
            results = await asyncio.gather(*jobs)
            # 稠密向量使用相似度阈值，BM25 只排除零分；二者过滤后才交给 RRF 融合。
            return [
                [row for row in rows if row["distance"] > 0]
                if sparse
                else [row for row in rows if row["distance"] >= settings.retrieval_min_score]
                for rows, sparse in zip(results, kinds, strict=True)
            ]

    tasks = [asyncio.create_task(branch(query)) for query in queries[:4]]
    try:
        results = await asyncio.gather(*tasks)
    finally:
        # 任一分支失败就取消兄弟分支，不能让失败请求在后台继续消耗资源。
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    rankings = [ranking for result in results for ranking in result]
    return rrf(rankings, settings.rag_candidate_limit), {
        "queries": len(tasks),
        "native_bm25": embeddings.collection in groups,
        "legacy_dense_documents": len(groups.get(embeddings.legacy_collection, {})),
        "recall_candidates": sum(len(ranking) for ranking in rankings),
    }


def materialize(
    db: Session,
    settings: Settings,
    workspace: str,
    user: str,
    base: str,
    scope: dict[str, tuple[int, str]],
    hits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """再次授权并校验 SQL 版本，达到子块覆盖率后替换为同版本父级原文。"""
    current = document_scope(db, settings, workspace, user, base)
    # 向量库只提供候选 ID；正文和归属以 SQL 为准，已撤销或已换代的命中会被丢弃。
    candidates: list[dict[str, Any]] = []
    for hit in hits:
        chunk = db.get(Chunk, hit["id"])
        doc = db.get(Document, chunk.document_id) if chunk else None
        if (
            not chunk
            or not doc
            or doc.id not in scope
            or current.get(doc.id) != scope[doc.id]
            or chunk.index_version != doc.index_version
        ):
            continue
        candidates.append(
            {
                "chunk_id": chunk.id,
                "document_id": doc.id,
                "filename": doc.filename,
                "page": chunk.page,
                "paragraph": chunk.ordinal,
                "content": chunk.content,
                "score": round(float(hit["rrf_score"]), 6),
                "index_version": doc.index_version,
                "embedding_version": doc.embedding_version,
                "collection_name": doc.collection_name,
                "parent_id": chunk.parent_id,
                "child_ids": [chunk.id],
            }
        )
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in candidates:
        if item["parent_id"]:
            groups[item["parent_id"]].append(item)
    merged: dict[str, dict[str, Any]] = {}
    for key, members in groups.items():
        parent = db.get(ParentChunk, key)
        if (
            not parent
            or parent.document_id != members[0]["document_id"]
            or parent.index_version != members[0]["index_version"]
        ):
            continue
        children = list(
            db.scalars(select(Chunk).where(Chunk.parent_id == key).order_by(Chunk.ordinal))
        )
        if len(children) != parent.child_count or any(
            child.document_id != parent.document_id or child.index_version != parent.index_version
            for child in children
        ):
            continue
        if len(members) >= max(2, math.ceil(parent.child_count * settings.rag_merge_ratio)):
            # 多个子块覆盖同一段的大部分内容时，改用父块原文，减少碎片并保留上下文。
            merged[key] = {
                **members[0],
                "content": parent.content,
                "child_ids": [child.id for child in children],
                "merged": True,
            }
    result = []
    emitted: set[str] = set()
    for item in candidates:
        parent_key = item["parent_id"]
        if parent_key in merged:
            if parent_key in emitted:
                continue
            emitted.add(parent_key)
            item = merged[parent_key]
        result.append({**item, "evidence_id": evidence_identity(workspace, base, item)})
    return result


def evidence_identity(workspace: str, base: str, item: dict[str, Any]) -> str:
    """绑定空间、资料、索引和模型版本，以及真实叶子或父级身份。"""
    node = item["parent_id"] if item.get("merged") else item["chunk_id"]
    identity = (
        f"{workspace}:{base}:{item['document_id']}:{item['index_version']}:"
        f"{item['embedding_version']}:{item['collection_name']}:{node}"
    )
    return "E-" + hashlib.sha256(identity.encode()).hexdigest()[:24]


def pack_evidence(candidates: list[dict[str, Any]], characters: int) -> list[dict[str, Any]]:
    """按证据身份和内容去重，在有界上下文内分配连续引用标签。"""
    seen: set[str] = set()
    contents: set[str] = set()
    result: list[dict[str, Any]] = []
    remaining = characters
    # 同一证据和重复正文分别去重；最后一个片段可截断，并明确标注 truncated。
    for item in candidates:
        content = str(item["content"])
        if item["evidence_id"] in seen or content in contents or remaining <= 0:
            continue
        seen.add(item["evidence_id"])
        contents.add(content)
        bounded = content[:remaining]
        result.append(
            {
                **item,
                "content": bounded,
                "truncated": len(bounded) < len(content),
                "label": f"S{len(result) + 1}",
            }
        )
        remaining -= len(bounded)
        if len(result) >= 5:
            break
    return result

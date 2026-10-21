"""知识库与主动记忆接口；读取、管理、原文下载分别实时校验权限。"""

import hashlib
from pathlib import PurePath
from typing import Annotated, Any, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from starlette.responses import Response

from app.accounts.access import AccountId, membership
from app.audit.service import audit
from app.core.pagination import page
from app.identity.dependencies import DB, Actor, Principal, Writer
from app.identity.models import User
from app.rag.embeddings import Embeddings
from app.rag.graph import queue_graph
from app.rag.graph_models import GraphBuild, GraphEntity, GraphFact
from app.rag.indexing import retrieve
from app.rag.models import Chunk, Document, KnowledgeBase, ParentChunk
from app.rag.retrieval import evidence_identity
from app.rag.storage import ObjectStorage

router = APIRouter(prefix="/api/v1", tags=["知识库与记忆"])


class BaseInput(BaseModel):
    """限定知识库名称的长度与输入字段。"""

    name: str = Field(min_length=1, max_length=80)


class SearchInput(BaseModel):
    """限定知识库检索问题的输入长度。"""

    query: str = Field(min_length=1, max_length=2000)


def owned_base(db: DB, key: str, actor: Principal, manage: bool = False) -> KnowledgeBase:
    """验证知识库归属及当前用户的空间成员资格。"""
    base = db.get(KnowledgeBase, key)
    if base is None:
        raise HTTPException(404)
    member = membership(db, base.account_id, actor.user.id)
    if manage and member.role not in {"owner", "admin"}:
        raise HTTPException(403)
    return base


def document_data(doc: Document) -> dict[str, object]:
    """输出资料的公开状态与处理进度，隐藏存储凭证。"""
    return {
        "id": doc.id,
        "filename": doc.filename,
        "size": doc.size,
        "status": doc.status,
        "error_code": doc.error_code,
        "progress": doc.progress,
        "index_version": doc.index_version,
        "embedding_version": doc.embedding_version,
        "content_hash": doc.content_hash,
        "chunk_strategy": doc.chunk_strategy,
    }


@router.get("/me/knowledge-bases")
def bases(
    account_id: AccountId,
    db: DB,
    actor: Actor,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: str | None = None,
) -> dict[str, Any]:
    """分页返回当前用户可访问空间内的知识库。"""
    membership(db, str(account_id), actor.user.id)
    query = select(KnowledgeBase).where(KnowledgeBase.account_id == str(account_id))
    if cursor:
        query = query.where(KnowledgeBase.id > cursor)
    return page(
        [
            {"id": row.id, "name": row.name}
            for row in db.scalars(query.order_by(KnowledgeBase.id).limit(limit + 1))
        ],
        limit,
    )


@router.post("/me/knowledge-bases", status_code=201)
def create_base(account_id: AccountId, data: BaseInput, db: DB, actor: Writer) -> dict[str, Any]:
    """验证空间权限后创建知识库并记录审计。"""
    member = membership(db, str(account_id), actor.user.id)
    if member.role not in {"owner", "admin"}:
        raise HTTPException(403)
    if not data.name.strip():
        raise HTTPException(422)
    row = KnowledgeBase(account_id=str(account_id), name=data.name.strip())
    db.add(row)
    db.flush()
    audit(db, actor.user, "knowledge.create", row.account_id, row.id)
    db.commit()
    return {"id": row.id, "name": row.name}


@router.get("/knowledge-bases/{base_id}/documents")
def documents(
    base_id: UUID,
    db: DB,
    actor: Actor,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: str | None = None,
) -> dict[str, Any]:
    """返回知识库中有效资料的公开状态，排除已删除条目。"""
    owned_base(db, str(base_id), actor)
    query = select(Document).where(Document.knowledge_base_id == str(base_id))
    if cursor:
        query = query.where(Document.id > cursor)
    return page(
        [document_data(row) for row in db.scalars(query.order_by(Document.id).limit(limit + 1))],
        limit,
    )


@router.post("/knowledge-bases/{base_id}/documents", status_code=202)
async def upload(
    base_id: UUID,
    request: Request,
    db: DB,
    actor: Writer,
    filename: Annotated[str, Query(min_length=1, max_length=160)],
    chunk_strategy: Annotated[Literal["fixed", "markdown_sections"], Query()] = "fixed",
) -> dict[str, Any]:
    """验证文件格式、大小与去重边界，保存资料并投递索引任务。"""
    base = owned_base(db, str(base_id), actor, True)
    if chunk_strategy == "markdown_sections" and PurePath(filename).suffix.lower() != ".md":
        raise HTTPException(422, detail={"code": "document_chunk_strategy_invalid"})
    if (
        PurePath(filename).name != filename
        or any(character in filename for character in "/\\\x00\r\n")
        or PurePath(filename).suffix.lower() not in {".txt", ".md", ".pdf"}
    ):
        raise HTTPException(422, detail={"code": "document_file_invalid"})
    content = bytearray()
    # 边收边检查大小，不能先完整读取再拒绝，否则超大上传仍会耗尽进程内存。
    async for block in request.stream():
        if len(content) + len(block) > 20 * 1024 * 1024:
            raise HTTPException(413)
        content.extend(block)
    if not content or not content.strip():
        raise HTTPException(422, detail={"code": "document_file_invalid"})
    # 空间锁序与任务创建一致，去重和总资料数检查不会发生并发竞争。
    db.scalar(select(User).where(User.id == base.account_id).with_for_update())
    digest = hashlib.sha256(content).hexdigest()
    existing = db.scalar(
        select(Document).where(
            Document.knowledge_base_id == base.id, Document.content_hash == digest
        )
    )
    if existing and existing.status == "deleted":
        db.delete(existing)
        db.flush()
        existing = None
    if existing:
        # 内容相同可复用已有导入；切块策略不同则冲突，不能把两种索引语义混为一份。
        existing_strategy = (
            "markdown_sections"
            if existing.chunk_strategy == "markdown-sections-700-parent-v1"
            else "fixed"
        )
        if existing_strategy != chunk_strategy:
            raise HTTPException(409, detail={"code": "document_chunk_strategy_conflict"})
        return {**document_data(existing), "duplicate": True}
    count = (
        db.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.knowledge_base_id == base.id, Document.status != "deleted")
        )
        or 0
    )
    if count >= 200:
        raise HTTPException(409, detail={"code": "knowledge_document_limit"})
    key = str(uuid4())
    embedding = Embeddings(request.app.state.settings)
    doc = Document(
        id=key,
        account_id=base.account_id,
        knowledge_base_id=base.id,
        filename=filename,
        content_hash=digest,
        object_key=f"knowledge/{base.account_id}/{key}",
        size=len(content),
        embedding_version=embedding.version,
        collection_name=embedding.collection,
        chunk_strategy=(
            "markdown-sections-700-parent-v1"
            if chunk_strategy == "markdown_sections"
            else "paragraph-700-overlap100-v1"
        ),
    )
    storage = ObjectStorage(request.app.state.settings)
    # 对象存储无法参加 SQL 事务，先存原文再提交元数据；SQL 失败时尽力删除孤立对象。
    try:
        storage.put(doc.object_key, bytes(content))
        db.add(doc)
        db.flush()
        audit(db, actor.user, "document.upload", base.account_id, key)
        db.commit()
    except Exception:
        db.rollback()
        try:
            storage.remove(doc.object_key)
        except Exception:
            pass
        raise HTTPException(503) from None
    try:
        request.app.state.dispatch_document(key, doc.index_version)
    except Exception:
        # 排队状态已持久化，调度器补投；不把暂时的 Redis 错误变成丢失任务。
        pass
    return document_data(doc)


@router.post("/knowledge-bases/{base_id}/search")
def search(
    base_id: UUID, data: SearchInput, request: Request, db: DB, actor: Writer
) -> dict[str, Any]:
    """在已授权知识库内检索资料分块并返回可核验的来源。"""
    base = owned_base(db, str(base_id), actor)
    return {
        "items": retrieve(
            db, request.app.state.settings, base.account_id, actor.user.id, base.id, data.query
        )
    }


@router.post("/documents/{document_id}/retry")
def retry(document_id: UUID, request: Request, db: DB, actor: Writer) -> dict[str, Any]:
    """验证权限与当前状态后重新提交处理意图，保留幂等和版本边界。"""
    doc = db.scalar(select(Document).where(Document.id == str(document_id)).with_for_update())
    if doc is None:
        raise HTTPException(404)
    owned_base(db, doc.knowledge_base_id, actor, True)
    cleanup = doc.status == "deleting"
    if not cleanup and doc.status not in {"failed", "ready"}:
        raise HTTPException(409)
    if not cleanup:
        embedding = Embeddings(request.app.state.settings)
        if doc.collection_name != embedding.collection:
            # 先移除旧模型索引，失败时不改变当前文档配置。
            from app.rag.embeddings import VectorIndex

            VectorIndex(request.app.state.settings).remove(doc.collection_name, doc.id)
        doc.index_version += 1
        doc.embedding_version, doc.collection_name = embedding.version, embedding.collection
        doc.status = "queued"
        # 新索引版本从零计数，旧任务不得覆盖新版本的进度。
        doc.progress = {}
        doc.attempts, doc.last_dispatched_at = 0, None
        doc.lease_token, doc.lease_until = None, None
    doc.error_code = None
    audit(db, actor.user, "document.retry", doc.account_id, doc.id)
    db.commit()
    try:
        request.app.state.dispatch_document(doc.id, doc.index_version, cleanup)
    except Exception:
        pass
    return document_data(doc)


@router.delete("/documents/{document_id}", status_code=202)
def remove(document_id: UUID, request: Request, db: DB, actor: Writer) -> dict[str, Any]:
    """验证资料归属后标记删除，并清理对象与对应索引。"""
    doc = db.scalar(select(Document).where(Document.id == str(document_id)).with_for_update())
    if doc is None:
        raise HTTPException(404)
    owned_base(db, doc.knowledge_base_id, actor, True)
    if doc.status == "deleted":
        return document_data(doc)
    doc.status, doc.error_code = "deleting", None
    # 先提交不可检索状态并撤销租约，实际删除对象和向量由后台完成。
    doc.lease_token, doc.lease_until, doc.last_dispatched_at = None, None, None
    audit(db, actor.user, "document.delete", doc.account_id, doc.id)
    db.commit()
    try:
        request.app.state.dispatch_document(doc.id, doc.index_version, True)
    except Exception:
        doc.error_code = "queue_unavailable"
        db.commit()
    return document_data(doc)


@router.get("/documents/{document_id}/graph")
def graph_status(document_id: UUID, db: DB, actor: Actor) -> dict[str, Any]:
    """返回本人有效文档的图谱状态及有限原文关系，支持人工核对。"""
    doc = db.get(Document, str(document_id))
    if doc is None:
        raise HTTPException(404)
    owned_base(db, doc.knowledge_base_id, actor)
    build = db.get(GraphBuild, doc.id)
    if doc.status != "ready" or not build or build.index_version != doc.index_version:
        return {"status": "unavailable", "facts": []}
    result: dict[str, Any] = {
        "status": build.status,
        "error_code": build.error_code,
        "mode": build.mode,
        "index_version": build.index_version,
        "facts": [],
    }
    if build.status == "ready":
        entities = {
            item.id: item.name
            for item in db.scalars(
                select(GraphEntity).where(
                    GraphEntity.document_id == doc.id,
                    GraphEntity.index_version == doc.index_version,
                )
            )
        }
        facts = list(
            db.scalars(
                select(GraphFact)
                .where(
                    GraphFact.document_id == doc.id,
                    GraphFact.index_version == doc.index_version,
                )
                .order_by(GraphFact.id)
                .limit(101)
            )
        )
        result["entity_count"] = len(entities)
        result["has_more"] = len(facts) > 100
        result["facts"] = [
            {
                "source": entities[fact.source_id],
                "target": entities[fact.target_id],
                "relation": fact.relation,
                "quote": fact.quote,
                "chunk_id": fact.chunk_id,
            }
            for fact in facts[:100]
        ]
    return result


@router.post("/documents/{document_id}/graph", status_code=202)
def rebuild_graph(document_id: UUID, request: Request, db: DB, actor: Writer) -> dict[str, Any]:
    """已有资料按需构建或重试图谱，不重新嵌入且不修改文档版本。"""
    if not request.app.state.settings.rag_graph_enabled:
        raise HTTPException(409, "graph_disabled")
    doc = db.scalar(select(Document).where(Document.id == str(document_id)).with_for_update())
    if doc is None:
        raise HTTPException(404)
    owned_base(db, doc.knowledge_base_id, actor, True)
    if doc.status != "ready":
        raise HTTPException(409, "document_not_ready")
    build = db.get(GraphBuild, doc.id)
    if (
        build
        and build.index_version == doc.index_version
        and build.status in {"queued", "building"}
    ):
        return {"status": build.status}
    queue_graph(db, doc)
    audit(db, actor.user, "document.graph", doc.account_id, doc.id)
    db.commit()
    return {"status": "queued"}


@router.get("/sources/{chunk_id}")
def source(
    chunk_id: UUID,
    db: DB,
    actor: Actor,
    index_version: int | None = None,
    parent_id: UUID | None = None,
    evidence_id: Annotated[str | None, Query(max_length=26)] = None,
) -> dict[str, Any]:
    """重新验证来源权限和有效版本后返回分块原文。"""
    chunk = db.get(Chunk, str(chunk_id))
    doc = db.get(Document, chunk.document_id) if chunk else None
    if not doc or not chunk or doc.status != "ready" or doc.index_version != chunk.index_version:
        raise HTTPException(404)
    owned_base(db, doc.knowledge_base_id, actor)
    if index_version is not None and index_version != doc.index_version:
        raise HTTPException(404)
    content = chunk.content
    if parent_id:
        parent = db.get(ParentChunk, str(parent_id))
        if (
            not parent
            or chunk.parent_id != parent.id
            or parent.document_id != doc.id
            or parent.index_version != doc.index_version
        ):
            raise HTTPException(404)
        content = parent.content
    identity = evidence_identity(
        # 重新计算来源身份，防止历史引用在文档重建后悄悄指向不同内容。
        doc.account_id,
        doc.knowledge_base_id,
        {
            "document_id": doc.id,
            "index_version": doc.index_version,
            "embedding_version": doc.embedding_version,
            "collection_name": doc.collection_name,
            "chunk_id": chunk.id,
            "parent_id": str(parent_id),
            "merged": bool(parent_id),
        },
    )
    if evidence_id is not None and evidence_id != identity:
        raise HTTPException(404)
    return {
        "filename": doc.filename,
        "page": chunk.page,
        "paragraph": chunk.ordinal,
        "content": content,
        "evidence_id": identity,
    }


@router.get("/documents/{document_id}/download")
def download(document_id: UUID, request: Request, db: DB, actor: Actor) -> Response:
    """检查知识库访问权限后返回对应的原始资料。"""
    doc = db.get(Document, str(document_id))
    if doc is None or doc.status in {"deleting", "deleted"}:
        raise HTTPException(404)
    owned_base(db, doc.knowledge_base_id, actor)
    content = ObjectStorage(request.app.state.settings).get(doc.object_key)
    return Response(
        content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(doc.filename),
            "Cache-Control": "no-store",
        },
    )

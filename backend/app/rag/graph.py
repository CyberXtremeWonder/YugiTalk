"""轻量图谱：受限抽取、原文校验、原子发布和两跳原文召回。"""

import asyncio
import logging
import re
import time
from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, delete, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.entities import identifier, now
from app.core.logging import log_failure
from app.identity.models import User
from app.providers.client import ProviderFailure
from app.rag.graph_models import GraphBuild, GraphEntity, GraphFact
from app.rag.model import StructuredModel
from app.rag.models import Chunk, Document
from app.tasks.reliability import utc


class EntityInput(BaseModel):
    """只允许少量实体类型和明确出现在原文中的名称、别名。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=2, max_length=120)
    kind: Literal["person", "organization", "system", "product", "concept"]
    aliases: list[str] = Field(default_factory=list, max_length=5)


class FactInput(BaseModel):
    """关系端点引用本次抽取实体的序号，证据必须是连续原文。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    source: int = Field(ge=0)
    target: int = Field(ge=0)
    relation: Literal["负责", "属于", "依赖", "使用", "关联"]
    quote: str = Field(min_length=2, max_length=700)


class Extraction(BaseModel):
    """限制单分块的输出规模，未知字段或越界关系不能发布。"""

    model_config = ConfigDict(extra="forbid")
    entities: list[EntityInput] = Field(default_factory=list, max_length=12)
    facts: list[FactInput] = Field(default_factory=list, max_length=12)


def clear_graph(db: Session, document: str) -> None:
    """删除此文档的关系与实体，不影响其他文档提供的依据。"""
    db.execute(delete(GraphFact).where(GraphFact.document_id == document))
    db.execute(delete(GraphEntity).where(GraphEntity.document_id == document))


def queue_graph(db: Session, doc: Document) -> GraphBuild:
    """在文档锁保护下登记持久任务，旧执行令牌立即失效。"""
    build = db.get(GraphBuild, doc.id)
    if build is None:
        build = GraphBuild(document_id=doc.id, index_version=doc.index_version)
        db.add(build)
    build.index_version, build.status = doc.index_version, "queued"
    build.token, build.lease_until, build.dispatched_at = None, None, None
    build.error_code, build.attempts = None, 0
    return build


def valid_document(db: Session, doc: Document | None, generation: int) -> bool:
    """构建与发布都检查文档状态、代次及用户是否仍然有效。"""
    if not doc or doc.status != "ready" or doc.index_version != generation:
        return False
    owner = db.get(User, doc.account_id)
    return bool(owner and owner.active)


def build_graph(engine: Engine, settings: Settings, key: str, generation: int) -> None:
    """固定三分钟执行窗口，超时由调度器恢复；失败不改变文档可检索状态。"""
    if not settings.rag_graph_enabled:
        return
    token = identifier()
    with Session(engine) as db:
        doc = db.scalar(select(Document).where(Document.id == key).with_for_update())
        build = db.get(GraphBuild, key)
        if (
            not valid_document(db, doc, generation)
            or not build
            or build.index_version != generation
            or build.status != "queued"
        ):
            return
        build.status, build.token = "building", token
        build.lease_until = now() + timedelta(seconds=200)
        build.attempts += 1
        build.mode = "mock" if settings.model_provider == "mock" else "model"
        chunks = list(
            db.scalars(
                select(Chunk)
                .where(Chunk.document_id == key, Chunk.index_version == generation)
                .order_by(Chunk.ordinal)
                .limit(65)
            )
        )
        parts = [(chunk.id, chunk.content) for chunk in chunks]
        # 先复制本代原文并释放事务，模型抽取期间不持有文档锁；发布前再验证代数和令牌。
        db.commit()

    def guard(_event: str | None, _data: dict[str, object] | None, _cost: int) -> None:
        """每次模型调用及流式读取时检查失效文档和执行令牌。"""
        with Session(engine) as db:
            doc, build = db.get(Document, key), db.get(GraphBuild, key)
            if (
                not valid_document(db, doc, generation)
                or not build
                or build.token != token
                or build.status != "building"
                or not build.lease_until
                or utc(build.lease_until) <= now()
            ):
                raise ProviderFailure("graph_build_cancelled")

    async def extract() -> list[tuple[str, str, Extraction]]:
        """每块一次结构化抽取，总时长与块数有界，不发布截断的半成品。"""
        if len(parts) > 64:
            raise ProviderFailure("graph_document_limit")
        result = []
        deadline = time.monotonic() + 180
        model = StructuredModel(settings, guard)
        async with asyncio.timeout(180):
            for chunk_id, content in parts:
                guard(None, None, 0)
                extracted = (
                    Extraction()
                    if settings.model_provider == "mock"
                    else await model.call(Extraction, "graph", {"text": content}, deadline)
                )
                validate_extraction(content, extracted)
                result.append((chunk_id, content, extracted))
        return result

    try:
        extracted_parts = asyncio.run(extract())
        guard(None, None, 0)
        with Session(engine) as db:
            doc = db.scalar(select(Document).where(Document.id == key).with_for_update())
            build = db.get(GraphBuild, key)
            if (
                not valid_document(db, doc, generation)
                or not build
                or build.token != token
                or not build.lease_until
                or utc(build.lease_until) <= now()
            ):
                return
            clear_graph(db, key)
            # 全部分块都抽取并校验成功后才替换图谱，避免读到部分新实体和部分旧关系。
            entities: dict[tuple[str, str], GraphEntity] = {}
            for chunk_id, _content, extracted in extracted_parts:
                local = []
                for item in extracted.entities:
                    identity = (item.name.casefold(), item.kind)
                    # 文档内同名同类型实体复用 ID；关系端点仍按当前块的实体序号映射。
                    entity = entities.get(identity)
                    if entity is None:
                        entity = GraphEntity(
                            id=identifier(),
                            document_id=key,
                            index_version=generation,
                            name=item.name,
                            kind=item.kind,
                            aliases=item.aliases,
                        )
                        entities[identity] = entity
                        db.add(entity)
                    else:
                        entity.aliases = sorted(set(entity.aliases + item.aliases))[:5]
                    local.append(entity.id)
                db.flush()
                for fact in extracted.facts:
                    db.add(
                        GraphFact(
                            document_id=key,
                            index_version=generation,
                            chunk_id=chunk_id,
                            source_id=local[fact.source],
                            target_id=local[fact.target],
                            relation=fact.relation,
                            quote=fact.quote,
                        )
                    )
            build.status, build.error_code = "ready", None
            build.token, build.lease_until = None, None
            db.commit()
    except Exception as error:
        log_failure(logging.getLogger(__name__), "knowledge.graph", error, document_id=key)
        with Session(engine) as db:
            db.scalar(select(Document).where(Document.id == key).with_for_update())
            build = db.get(GraphBuild, key)
            if build and build.token == token:
                build.status = "failed"
                build.error_code = (
                    error.code if isinstance(error, ProviderFailure) else "graph_build_failed"
                )
                build.token, build.lease_until = None, None
                db.commit()


def validate_extraction(content: str, data: Extraction) -> None:
    """拒绝虚构引用、未出现的名称、无效端点和缺少双方名称的证据。"""
    for entity in data.entities:
        if entity.name not in content or any(
            len(alias) < 2 or len(alias) > 120 or alias not in content for alias in entity.aliases
        ):
            raise ProviderFailure("graph_evidence_invalid")
    for fact in data.facts:
        if (
            fact.source >= len(data.entities)
            or fact.target >= len(data.entities)
            or fact.source == fact.target
            or fact.quote not in content
        ):
            raise ProviderFailure("graph_evidence_invalid")
        for index in (fact.source, fact.target):
            entity = data.entities[index]
            if not any(name in fact.quote for name in [entity.name, *entity.aliases]):
                raise ProviderFailure("graph_evidence_invalid")


def dispatch_graph(key: str, generation: int) -> None:
    """队列只携带资料身份与代次，不携带原文。"""
    from app.tasks.celery_app import celery_app

    celery_app.send_task("yugitalk.knowledge.graph", args=[key, generation])


def sweep_graph(engine: Engine, settings: Settings) -> None:
    """补投持久任务并有限恢复失联任务，不自动抽取已有历史文档。"""
    if not settings.rag_graph_enabled:
        return
    with Session(engine) as db:
        builds = list(
            db.scalars(
                select(GraphBuild)
                .where(GraphBuild.status.in_(["queued", "building"]))
                .order_by(GraphBuild.document_id)
                .limit(100)
            )
        )
        keys = [build.document_id for build in builds]
    for key in keys:
        with Session(engine) as db:
            doc = db.scalar(select(Document).where(Document.id == key).with_for_update())
            build = db.get(GraphBuild, key)
            if not build or not valid_document(db, doc, build.index_version):
                if build:
                    build.status, build.token = "cancelled", None
                    db.commit()
                continue
            if build.status == "building":
                if build.lease_until and utc(build.lease_until) > now():
                    continue
                build.status = "queued" if build.attempts < 3 else "failed"
                build.error_code, build.token = "graph_worker_lost", None
            if build.status != "queued":
                db.commit()
                continue
            if build.dispatched_at and utc(build.dispatched_at) > now() - timedelta(seconds=30):
                continue
            build.dispatched_at = now()
            generation = build.index_version
            db.commit()
        try:
            dispatch_graph(key, generation)
        except Exception:
            # 持久状态仍为排队；下次扫描继续补投，队列故障不影响普通索引。
            pass


def matches(name: str, question: str) -> bool:
    """英文实体按词边界匹配，中文实体按完整名称匹配。"""
    if re.fullmatch(r"[\w .-]+", name, flags=re.ASCII):
        pattern = r"(?<![a-z0-9_])" + re.escape(name) + r"(?![a-z0-9_])"
        return bool(re.search(pattern, question, re.I))
    return name.casefold() in question.casefold()


def graph_recall(
    db: Session,
    scope: dict[str, tuple[int, str]],
    question: str,
    limit: int,
) -> list[dict[str, Any]]:
    """仅在已授权版本内扩展两跳，返回原文分块而非模型生成的关系文本。"""
    if not scope:
        return []
    entities = list(
        db.scalars(
            select(GraphEntity)
            .join(GraphBuild, GraphBuild.document_id == GraphEntity.document_id)
            .where(
                GraphEntity.document_id.in_(scope),
                GraphBuild.status == "ready",
                GraphBuild.index_version == GraphEntity.index_version,
            )
            .order_by(GraphEntity.id)
            .limit(10001)
        )
    )
    if len(entities) > 10000:
        return []
    nodes = {item.id: item for item in entities if item.index_version == scope[item.document_id][0]}
    frontier = {
        key
        for key, item in nodes.items()
        if any(matches(name, question) for name in [item.name, *item.aliases])
    }
    frontier = set(sorted(frontier)[:8])
    # 以问题直接提到的实体为起点，逐层扩展最多两跳；seen 阻止关系环无限遍历。
    seen = set(frontier)
    hits: dict[str, dict[str, Any]] = {}
    for _hop in range(2):
        if not frontier:
            break
        # 同名同类型只扩展候选，最终是否为同一对象仍交给原文证据判断。
        names = {(nodes[key].name.casefold(), nodes[key].kind) for key in frontier}
        frontier |= {
            key for key, item in nodes.items() if (item.name.casefold(), item.kind) in names
        }
        frontier = set(sorted(frontier)[:32])
        following: set[str] = set()
        for key in sorted(frontier):
            facts = db.scalars(
                select(GraphFact)
                .where(
                    GraphFact.document_id.in_(scope),
                    or_(GraphFact.source_id == key, GraphFact.target_id == key),
                )
                .order_by(GraphFact.id)
                .limit(8)
            )
            for fact in facts:
                if (
                    fact.index_version != scope[fact.document_id][0]
                    or fact.source_id not in nodes
                    or fact.target_id not in nodes
                ):
                    continue
                hits.setdefault(fact.chunk_id, {"id": fact.chunk_id})
                # 图关系只用来找到原文分块，最终答案仍引用 SQL 中的文档证据。
                following.update((fact.source_id, fact.target_id))
                if len(hits) >= limit:
                    return list(hits.values())
        seen.update(frontier)
        frontier = following - seen
    return list(hits.values())


def fuse_graph(
    db: Session,
    settings: Settings,
    scope: dict[str, tuple[int, str]],
    question: str,
    hits: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """无图谱时保持原排名，有图谱时按相同分块身份进行排名融合。"""
    from app.rag.retrieval import rrf

    if not settings.rag_graph_enabled:
        return hits, 0
    try:
        with db.begin_nested():
            graph_hits = graph_recall(db, scope, question, settings.rag_candidate_limit)
    except SQLAlchemyError:
        # 保存点隔离图谱查询失败，普通检索仍需经过后续权限与版本校验。
        return hits, -1
    return (
        rrf([hits, graph_hits], settings.rag_candidate_limit) if graph_hits else hits,
        len(graph_hits),
    )

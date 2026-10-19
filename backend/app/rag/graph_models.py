"""图谱构建状态与带原文依据的实体、关系；不替代文档授权。"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.entities import Base, identifier


class GraphBuild(Base):
    """每份文档独立发布图谱，固定租约约束重复任务与宕机恢复。"""

    __tablename__ = "graph_builds"
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), primary_key=True)
    index_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    mode: Mapped[str] = mapped_column(String(16), default="model")


class GraphEntity(Base):
    """实体在文档内归一；跨文档同名仅作为召回线索，不强行合并身份。"""

    __tablename__ = "graph_entities"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    index_version: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20))
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)


class GraphFact(Base):
    """每行是一条关系的一份原文依据，允许多文档分别支持相同关系。"""

    __tablename__ = "graph_facts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    index_version: Mapped[int] = mapped_column(Integer)
    source_id: Mapped[str] = mapped_column(ForeignKey("graph_entities.id"), index=True)
    target_id: Mapped[str] = mapped_column(ForeignKey("graph_entities.id"), index=True)
    relation: Mapped[str] = mapped_column(String(20))
    chunk_id: Mapped[str] = mapped_column(ForeignKey("document_chunks.id"), index=True)
    quote: Mapped[str] = mapped_column(Text)

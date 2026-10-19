"""知识库、资料与分块实体；关系数据库是资料可检索状态的权威来源。"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.entities import Base, identifier, now


class KnowledgeBase(Base):
    """保存用户账号内的知识库名称与归属。"""

    __tablename__ = "knowledge_bases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    account_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Document(Base):
    """保存原始资料、索引配置、处理进度及恢复租约。"""

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("knowledge_base_id", "content_hash", name="uq_document_hash"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    knowledge_base_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    filename: Mapped[str] = mapped_column(String(160))
    content_hash: Mapped[str] = mapped_column(String(64))
    object_key: Mapped[str] = mapped_column(String(200))
    size: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    progress: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    index_version: Mapped[int] = mapped_column(Integer, default=1)
    embedding_version: Mapped[str] = mapped_column(String(64))
    chunk_strategy: Mapped[str] = mapped_column(String(40), default="paragraph-700-overlap100-v1")
    collection_name: Mapped[str] = mapped_column(String(64))
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ParentChunk(Base):
    """保存可核验的父级原文范围；父块只存 SQL，不重复写入向量索引。"""

    __tablename__ = "document_parent_chunks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    index_version: Mapped[int] = mapped_column(Integer)
    ordinal: Mapped[int] = mapped_column(Integer)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content: Mapped[str] = mapped_column(Text)
    child_count: Mapped[int] = mapped_column(Integer)


class Chunk(Base):
    """保存资料分块内容、位置和索引版本，用于校验来源。"""

    __tablename__ = "document_chunks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content: Mapped[str] = mapped_column(Text)
    index_version: Mapped[int] = mapped_column(Integer)
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_parent_chunks.id"), nullable=True, index=True
    )

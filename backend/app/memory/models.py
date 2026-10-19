"""资料、引用与偏好模型；PostgreSQL 是文档可检索状态的唯一权威。"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.entities import Base, identifier, now


class MemoryPreference(Base):
    """记录用户是否允许保存和使用长期记忆。"""

    __tablename__ = "memory_preferences"
    account_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Memory(Base):
    """保存用户在空间内主动确认的偏好记忆。"""

    __tablename__ = "memories"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    account_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    content: Mapped[str] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ConversationSummary(Base):
    """保存有界对话摘要及其覆盖的消息范围。"""

    __tablename__ = "conversation_summaries"
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    upto_sequence: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    source_ids: Mapped[list[str]] = mapped_column(JSON)
    strategy: Mapped[str] = mapped_column(String(40), default="bounded-extractive-v1")

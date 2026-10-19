"""身份与用户资源关系模型；类型、唯一约束和外键共同保护一致性。"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.entities import Base, identifier, now


class Agent(Base):
    """空间共享的 Agent 身份，发布配置另存不可变版本。"""

    __tablename__ = "agents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    account_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AgentVersion(Base):
    """保存不可变的助手配置及版本序号，供任务固定引用。"""

    __tablename__ = "agent_versions"
    __table_args__ = (UniqueConstraint("agent_id", "number", name="uq_agent_version_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    config: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

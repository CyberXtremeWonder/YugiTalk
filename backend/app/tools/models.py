"""空间工具启停与不可变 HTTP 查询工具版本。"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.entities import Base, identifier, now


class ManagedTool(Base):
    """保存空间工具目录状态；内置工具只有启停覆盖，自定义工具保留历史版本。"""

    __tablename__ = "managed_tools"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    account_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        Index(
            "uq_managed_tool_name",
            "account_id",
            "name",
            unique=True,
            postgresql_where=text("deleted = false"),
            sqlite_where=text("deleted = 0"),
        ),
    )


class ToolVersion(Base):
    """冻结输入参数、网络端点和角色策略，不保存任何真实凭证。"""

    __tablename__ = "tool_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    tool_id: Mapped[str] = mapped_column(ForeignKey("managed_tools.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (Index("uq_tool_version_number", "tool_id", "number", unique=True),)


class ToolSubmission(Base):
    """保存不可变待审声明及审核结论，未批准的声明不会进入执行目录。"""

    __tablename__ = "tool_submissions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    account_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    submitter_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    reason: Mapped[str] = mapped_column(String(500), default="")
    reviewer_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

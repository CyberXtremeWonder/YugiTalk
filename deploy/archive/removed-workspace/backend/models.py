"""工作空间与成员角色实体。"""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.entities import Base, identifier


class Workspace(Base):
    """保存工作空间的名称与创建时间。"""

    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    name: Mapped[str] = mapped_column(String(80))
    # 删除后隐藏空间并阻止访问，保留资料与审计供后续清理。
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Membership(Base):
    """保存用户与空间的关联及所属角色。"""

    __tablename__ = "memberships"
    __table_args__ = (
        CheckConstraint("role IN ('owner','admin','member')", name="membership_role"),
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(10))

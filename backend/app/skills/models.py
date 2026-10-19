"""空间共享的技能身份与不可变技能版本。"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.entities import Base, identifier, now


class Skill(Base):
    """保存技能归属及启停状态，删除采用软删除以保留任务的版本引用。"""

    __tablename__ = "skills"
    __table_args__ = (
        Index(
            "uq_skill_workspace_name",
            "account_id",
            "name",
            unique=True,
            postgresql_where=text("deleted = false"),
            sqlite_where=text("deleted = 0"),
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    account_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SkillVersion(Base):
    """保存完整说明与内容哈希，后续编辑发布新版本而不覆盖旧任务的内容。"""

    __tablename__ = "skill_versions"
    __table_args__ = (Index("uq_skill_version_number", "skill_id", "number", unique=True),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(500))
    instructions: Mapped[str] = mapped_column(Text)
    allowed_tools: Mapped[list[str]] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

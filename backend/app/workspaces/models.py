"""按用户保存文件工作空间，不把读取根目录交由模型决定。"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.entities import Base


class UserWorkspace(Base):
    """目录配置独立于模型调用，修改版本用于阻止旧任务复活权限。"""

    __tablename__ = "user_workspaces"
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    directory: Mapped[str] = mapped_column(String(2048), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(String(16), default="server")
    files: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

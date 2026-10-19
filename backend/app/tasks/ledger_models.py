"""工具执行账本与恢复记录；旧审批和工单映射仅为保留历史数据。"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.entities import Base, identifier, now


class ToolExecution(Base):
    """记录工具参数摘要、执行状态与结果，提供恢复去重依据。"""

    __tablename__ = "tool_executions"
    __table_args__ = (UniqueConstraint("run_id", "call_key", name="uq_tool_run_call"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    call_key: Mapped[str] = mapped_column(String(180))
    tool: Mapped[str] = mapped_column(String(80))
    arguments: Mapped[dict[str, object]] = mapped_column(JSON)
    argument_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="proposed")
    result: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)


class Approval(Base):
    """保留历史审批数据的映射，当前功能不提供审批操作。"""

    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    execution_id: Mapped[str] = mapped_column(
        ForeignKey("tool_executions.id", ondelete="CASCADE"), unique=True
    )
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    argument_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Ticket(Base):
    """保留历史工单数据的映射，当前功能不提供工单创建。"""

    __tablename__ = "tickets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    account_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    # 保留旧版本唯一约束，不删除用户历史数据；当前应用不提供工单操作。
    execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("tool_executions.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    title: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RunRetry(Base):
    """绑定用户幂等键与任务，防止重复重试请求再次预留预算。"""

    __tablename__ = "run_retries"
    __table_args__ = (UniqueConstraint("owner_id", "key", name="uq_retry_owner_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    key: Mapped[str] = mapped_column(String(128))

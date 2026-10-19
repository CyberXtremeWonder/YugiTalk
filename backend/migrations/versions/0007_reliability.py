"""任务检查点、租约、审批和副作用账本；升级保留已有资料与会话。"""

import sqlalchemy as sa
from alembic import op

revision = "0007_reliability"
down_revision = "0006_document_progress"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """应用当前版本的数据库结构变更，供迁移工具顺序执行。"""
    for table in ("runs", "documents"):
        op.add_column(table, sa.Column("lease_token", sa.String(36)))
        op.add_column(table, sa.Column("lease_until", sa.DateTime(timezone=True)))
        op.add_column(table, sa.Column("last_dispatched_at", sa.DateTime(timezone=True)))
        op.add_column(
            table, sa.Column("attempts", sa.Integer(), nullable=False, server_default="0")
        )
    for name in ("state_version", "active_seconds", "settled_tokens"):
        op.add_column("runs", sa.Column(name, sa.Integer(), nullable=False, server_default="0"))
    op.add_column("runs", sa.Column("checkpoint", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("runs", sa.Column("slice_started_at", sa.DateTime(timezone=True)))
    # 旧版本终态已经结算；重试不得再次计入旧成本。
    op.execute(
        "UPDATE runs SET settled_tokens=charged_tokens "
        "WHERE status IN ('succeeded','failed','cancelled')"
    )
    op.create_table(
        "tool_executions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id", sa.String(36), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("call_key", sa.String(180), nullable=False),
        sa.Column("tool", sa.String(80), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("argument_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.UniqueConstraint("run_id", "call_key", name="uq_tool_run_call"),
    )
    op.create_index("ix_tool_executions_run_id", "tool_executions", ["run_id"])
    op.create_table(
        "approvals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "execution_id",
            sa.String(36),
            sa.ForeignKey("tool_executions.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("argument_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_approvals_owner_id", "approvals", ["owner_id"])
    op.create_table(
        "tickets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "execution_id",
            sa.String(36),
            sa.ForeignKey("tool_executions.id", ondelete="SET NULL"),
            unique=True,
        ),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tickets_workspace_id", "tickets", ["workspace_id"])
    op.create_index("ix_tickets_owner_id", "tickets", ["owner_id"])
    op.create_table(
        "run_retries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id", sa.String(36), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.UniqueConstraint("owner_id", "key", name="uq_retry_owner_key"),
    )
    op.create_index("ix_run_retries_run_id", "run_retries", ["run_id"])


def downgrade() -> None:
    """撤销当前版本的数据库结构变更，供迁移工具显式回退。"""
    for table in ("run_retries", "tickets", "approvals", "tool_executions"):
        op.drop_table(table)
    for name in (
        "slice_started_at",
        "checkpoint",
        "settled_tokens",
        "active_seconds",
        "state_version",
    ):
        op.drop_column("runs", name)
    for table in ("documents", "runs"):
        for name in ("attempts", "last_dispatched_at", "lease_until", "lease_token"):
            op.drop_column(table, name)

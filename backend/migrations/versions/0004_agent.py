"""Agent 版本、私有会话、任务事件与预算表；仅新增业务表。"""

import sqlalchemy as sa
from alembic import op

revision = "0004_agent"
down_revision = "0003_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """应用当前版本的数据库结构变更，供迁移工具顺序执行。"""
    op.create_table(
        "agents",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "workspace_id", sa.String(length=36), sa.ForeignKey("workspaces.id"), nullable=False
        ),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agents_workspace_id", "agents", ["workspace_id"])
    op.create_table(
        "agent_versions",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("agent_id", sa.String(length=36), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("agent_id", "number", name="uq_agent_version_number"),
    )
    op.create_index("ix_agent_versions_agent_id", "agent_versions", ["agent_id"])
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "workspace_id", sa.String(length=36), sa.ForeignKey("workspaces.id"), nullable=False
        ),
        sa.Column("owner_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_conversations_owner_id", "conversations", ["owner_id"])
    op.create_index("ix_conversations_workspace_id", "conversations", ["workspace_id"])
    op.create_table(
        "messages",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "conversation_id",
            sa.String(length=36),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=12), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id", "sequence", name="uq_message_sequence"),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_table(
        "runs",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "workspace_id", sa.String(length=36), sa.ForeignKey("workspaces.id"), nullable=False
        ),
        sa.Column("owner_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "conversation_id",
            sa.String(length=36),
            sa.ForeignKey("conversations.id"),
            nullable=False,
        ),
        sa.Column(
            "agent_version_id",
            sa.String(length=36),
            sa.ForeignKey("agent_versions.id"),
            nullable=False,
        ),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False),
        sa.Column("charged_tokens", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("error_code", sa.String(length=50)),
        sa.Column("event_sequence", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("owner_id", "idempotency_key", name="uq_run_idempotency"),
    )
    op.create_index("ix_runs_conversation_id", "runs", ["conversation_id"])
    op.create_index("ix_runs_owner_id", "runs", ["owner_id"])
    op.create_index("ix_runs_status", "runs", ["status"])
    op.create_index("ix_runs_workspace_id", "runs", ["workspace_id"])
    op.create_table(
        "run_events",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "run_id",
            sa.String(length=36),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "sequence", name="uq_run_event_sequence"),
    )
    op.create_index("ix_run_events_run_id", "run_events", ["run_id"])
    op.create_table(
        "budgets",
        sa.Column("key", sa.String(length=80), primary_key=True, nullable=False),
        sa.Column("reserved", sa.Integer(), nullable=False),
        sa.Column("spent", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    """撤销当前版本的数据库结构变更，供迁移工具显式回退。"""
    for table in (
        "budgets",
        "run_events",
        "runs",
        "messages",
        "conversations",
        "agent_versions",
        "agents",
    ):
        op.drop_table(table)

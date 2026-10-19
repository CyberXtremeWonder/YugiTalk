"""新增独立任务步骤明细，不改变历史任务和预算。"""

import sqlalchemy as sa
from alembic import op

revision = "0016_task_traces"
down_revision = "0015_user_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为新任务记录步骤和实际供应商用量。"""
    op.create_table(
        "trace_steps",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id", sa.String(36), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("parent_id", sa.String(36)),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("iteration", sa.Integer()),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("details", sa.JSON(), nullable=False),
    )
    op.create_index("ix_trace_steps_run_id", "trace_steps", ["run_id"])


def downgrade() -> None:
    """仅移除步骤记录表。"""
    op.drop_table("trace_steps")

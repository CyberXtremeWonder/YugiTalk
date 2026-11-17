"""对话附件随消息独立保留，并从仍存在的任务快照回填历史资料。"""

import sqlalchemy as sa
from alembic import op

revision = "0026_message_uploads"
down_revision = "0025_workspace_import"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保留旧消息及任务；已被清理的快照不伪造回填内容。"""
    op.add_column(
        "messages",
        sa.Column(
            "uploaded_files",
            sa.JSON(),
            nullable=False,
            server_default="[]",
        ),
    )
    messages = sa.table(
        "messages",
        sa.column("conversation_id", sa.String()),
        sa.column("sequence", sa.Integer()),
        sa.column("role", sa.String()),
        sa.column("uploaded_files", sa.JSON()),
    )
    runs = sa.table(
        "runs",
        sa.column("conversation_id", sa.String()),
        sa.column("snapshot", sa.JSON()),
    )
    source = (
        sa.select(runs.c.snapshot["uploaded_files"])
        .where(
            runs.c.conversation_id == messages.c.conversation_id,
            runs.c.snapshot["conversation_upto_sequence"].as_integer() == messages.c.sequence,
            runs.c.snapshot["uploaded_files"].as_string().is_not(None),
        )
        .correlate(messages)
        .limit(1)
    )
    op.execute(
        sa.update(messages)
        .where(
            messages.c.role == "user",
            source.exists(),
        )
        .values(uploaded_files=source.scalar_subquery())
    )
    summaries = sa.table(
        "conversation_summaries",
        sa.column("conversation_id", sa.String()),
        sa.column("upto_sequence", sa.Integer()),
    )
    # 旧摘要没有接收附件正文，下一轮须从保留的原消息重新生成。
    op.execute(
        sa.delete(summaries).where(
            sa.select(1)
            .where(
                messages.c.conversation_id == summaries.c.conversation_id,
                messages.c.role == "user",
                messages.c.sequence <= summaries.c.upto_sequence,
                sa.cast(messages.c.uploaded_files, sa.Text()) != "[]",
            )
            .exists()
        )
    )


def downgrade() -> None:
    """仅移除本次新增字段，任务快照保持原样。"""
    op.drop_column("messages", "uploaded_files")

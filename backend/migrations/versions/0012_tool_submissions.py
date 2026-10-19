"""增加用户工具声明提交和管理员审核记录。"""

import sqlalchemy as sa
from alembic import op

revision = "0012_tool_submissions"
down_revision = "0011_email_registration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增独立审核表，不改动已有工具版本和任务授权快照。"""
    op.create_table(
        "tool_submissions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("submitter_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("reviewer_id", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    for name in ("workspace_id", "submitter_id", "status"):
        op.create_index("ix_tool_submissions_" + name, "tool_submissions", [name])


def downgrade() -> None:
    """删除审核表；真实环境先保留审核历史，不自动回退数据库。"""
    op.drop_table("tool_submissions")

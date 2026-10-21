"""知识库软删除，保留资料记录供后台清理原文和索引。"""

import sqlalchemy as sa
from alembic import op

revision = "0019_knowledge_base_deletion"
down_revision = "0018_agent_deletion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为已有知识库添加默认未删除的标记。"""
    op.add_column(
        "knowledge_bases",
        sa.Column("deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    """仅移除删除标记。"""
    op.drop_column("knowledge_bases", "deleted")

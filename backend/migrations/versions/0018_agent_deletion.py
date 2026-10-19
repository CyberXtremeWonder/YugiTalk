"""助手软删除，保留历史版本与任务引用。"""

import sqlalchemy as sa
from alembic import op

revision = "0018_agent_deletion"
down_revision = "0017_knowledge_graph"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """已有助手默认保持可用，仅新增删除标记。"""
    op.add_column(
        "agents", sa.Column("deleted", sa.Boolean(), nullable=False, server_default=sa.false())
    )


def downgrade() -> None:
    """回滚仅移除删除标记，不删除助手、版本或任务数据。"""
    op.drop_column("agents", "deleted")

"""保存文档导入的实际阶段与分块计数；保留已有文档与索引。"""

import sqlalchemy as sa
from alembic import op

revision = "0006_document_progress"
down_revision = "0005_knowledge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """应用当前版本的数据库结构变更，供迁移工具顺序执行。"""
    op.add_column(
        "documents", sa.Column("progress", sa.JSON(), nullable=False, server_default="{}")
    )


def downgrade() -> None:
    """撤销当前版本的数据库结构变更，供迁移工具显式回退。"""
    op.drop_column("documents", "progress")

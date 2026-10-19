"""为工作空间增加软删除时间，保留已有业务数据。"""

import sqlalchemy as sa
from alembic import op

revision = "0014_workspace_deletion"
down_revision = "0013_private_tools"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增可空删除时间，已有空间默认继续可用。"""
    op.add_column("workspaces", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """存在已删除空间时拒绝回退，避免重新开放已撤销的访问。"""
    connection = op.get_bind()
    if connection.execute(
        sa.text("SELECT COUNT(*) FROM workspaces WHERE deleted_at IS NOT NULL")
    ).scalar():
        raise RuntimeError("存在已删除工作空间，不能移除删除标记")
    op.drop_column("workspaces", "deleted_at")

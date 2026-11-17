"""为浏览器选择的目录保存按用户隔离的文件快照清单。"""

import sqlalchemy as sa
from alembic import op

revision = "0025_workspace_import"
down_revision = "0024_user_workspace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """已有目录配置保留原语义，不自动启用文件导入。"""
    op.add_column("user_workspaces", sa.Column(
        "source", sa.String(16), nullable=False, server_default="server",
    ))
    op.add_column("user_workspaces", sa.Column(
        "files", sa.JSON(), nullable=False, server_default="{}",
    ))
    op.add_column("user_workspaces", sa.Column(
        "imported_at", sa.DateTime(timezone=True), nullable=True,
    ))


def downgrade() -> None:
    """只移除快照元数据，不触碰本机文件。"""
    op.drop_column("user_workspaces", "imported_at")
    op.drop_column("user_workspaces", "files")
    op.drop_column("user_workspaces", "source")

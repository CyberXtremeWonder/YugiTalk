"""为每个用户保存默认关闭的文件工作空间配置。"""

import sqlalchemy as sa
from alembic import op

revision = "0024_user_workspace"
down_revision = "0023_research"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增配置表，不自动选择目录或向现有用户授予文件读取权限。"""
    op.create_table(
        "user_workspaces",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("directory", sa.String(2048), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    """仅移除工作空间设置，不触碰用户本机文件。"""
    op.drop_table("user_workspaces")

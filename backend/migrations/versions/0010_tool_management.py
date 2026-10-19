"""新增空间工具状态与不可变查询版本，不改变原有消息、技能和知识库。"""

import sqlalchemy as sa
from alembic import op

revision = "0010_tool_management"
down_revision = "0009_custom_skills"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建工具目录、版本和空间唯一索引，保留软删除后的历史引用。"""
    op.create_table(
        "managed_tools",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("builtin", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_managed_tools_workspace_id", "managed_tools", ["workspace_id"])
    op.create_index(
        "uq_managed_tool_name",
        "managed_tools",
        ["workspace_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted = false"),
        sqlite_where=sa.text("deleted = 0"),
    )
    op.create_table(
        "tool_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tool_id", sa.String(36), sa.ForeignKey("managed_tools.id"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tool_versions_tool_id", "tool_versions", ["tool_id"])
    op.create_index("uq_tool_version_number", "tool_versions", ["tool_id", "number"], unique=True)


def downgrade() -> None:
    """移除本次新增表；真实环境回退前须独立备份工具配置和审计数据。"""
    op.drop_table("tool_versions")
    op.drop_table("managed_tools")

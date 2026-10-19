"""新增空间技能及不可变版本，不改动已有任务和知识库数据。"""

import sqlalchemy as sa
from alembic import op

revision = "0009_custom_skills"
down_revision = "0008_rag_hierarchy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建技能目录、版本和唯一索引，名称只对未删除的技能保持唯一。"""
    op.create_table(
        "skills",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_skills_workspace_id", "skills", ["workspace_id"])
    op.create_index("ix_skills_owner_id", "skills", ["owner_id"])
    op.create_index(
        "uq_skill_workspace_name",
        "skills",
        ["workspace_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted = false"),
        sqlite_where=sa.text("deleted = 0"),
    )
    op.create_table(
        "skill_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("skill_id", sa.String(36), sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("allowed_tools", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_skill_versions_skill_id", "skill_versions", ["skill_id"])
    op.create_index(
        "uq_skill_version_number", "skill_versions", ["skill_id", "number"], unique=True
    )


def downgrade() -> None:
    """移除本次新增表；回退会丢失自定义技能，正式操作前必须独立备份。"""
    op.drop_table("skill_versions")
    op.drop_table("skills")

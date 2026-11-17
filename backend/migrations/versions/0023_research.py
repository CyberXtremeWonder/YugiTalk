"""研究项目、来源与报告。"""

import sqlalchemy as sa
from alembic import op

revision = "0023_research"
down_revision = "0022_large_document_size"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_projects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_research_projects_owner_id", "research_projects", ["owner_id"])
    op.create_table(
        "research_sources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id", sa.String(36), sa.ForeignKey("research_projects.id"), nullable=False
        ),
        sa.Column("identity", sa.String(64), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "identity", name="uq_research_source"),
    )
    op.create_index("ix_research_sources_project_id", "research_sources", ["project_id"])
    op.create_table(
        "research_reports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id", sa.String(36), sa.ForeignKey("research_projects.id"), nullable=False
        ),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("markdown", sa.Text(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "run_id", name="uq_research_report_run"),
    )
    op.create_index("ix_research_reports_project_id", "research_reports", ["project_id"])


def downgrade() -> None:
    op.drop_table("research_reports")
    op.drop_table("research_sources")
    op.drop_table("research_projects")

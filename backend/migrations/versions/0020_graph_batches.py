"""暂存图谱分批抽取结果，支持大文档与后台任务恢复。"""

import sqlalchemy as sa
from alembic import op

revision = "0020_graph_batches"
down_revision = "0019_knowledge_base_deletion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建独立于已发布图谱的分块结果表。"""
    op.create_table(
        "graph_parts",
        sa.Column("chunk_id", sa.String(36), sa.ForeignKey("document_chunks.id"), primary_key=True),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
    )
    op.create_index("ix_graph_parts_document_id", "graph_parts", ["document_id"])


def downgrade() -> None:
    """移除临时结果，保留文档与已发布的图谱。"""
    op.drop_table("graph_parts")

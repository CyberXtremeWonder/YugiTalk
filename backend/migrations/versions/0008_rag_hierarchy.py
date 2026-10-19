"""增加父子分块关系；旧资料保持可读，混合索引通过单独重建切换。"""

import sqlalchemy as sa
from alembic import op

revision = "0008_rag_hierarchy"
down_revision = "0007_reliability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """添加父级原文表和可空外键，不修改既有分块或 Milvus 集合。"""
    op.create_table(
        "document_parent_chunks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("index_version", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("child_count", sa.Integer(), nullable=False),
    )
    op.create_index(
        "ix_document_parent_chunks_document_id", "document_parent_chunks", ["document_id"]
    )
    with op.batch_alter_table("document_chunks") as batch:
        batch.add_column(sa.Column("parent_id", sa.String(36), nullable=True))
        batch.create_foreign_key("fk_chunk_parent", "document_parent_chunks", ["parent_id"], ["id"])
        batch.create_index("ix_document_chunks_parent_id", ["parent_id"])


def downgrade() -> None:
    """移除层级结构；不删除资料原文或既有向量集合。"""
    with op.batch_alter_table("document_chunks") as batch:
        batch.drop_index("ix_document_chunks_parent_id")
        batch.drop_constraint("fk_chunk_parent", type_="foreignkey")
        batch.drop_column("parent_id")
    op.drop_table("document_parent_chunks")

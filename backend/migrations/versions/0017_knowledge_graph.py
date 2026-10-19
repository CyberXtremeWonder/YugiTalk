"""新增可独立重试的知识图谱，不修改已有文档和向量索引。"""

import sqlalchemy as sa
from alembic import op

revision = "0017_knowledge_graph"
down_revision = "0016_task_traces"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """建立构建状态、文档内实体和逐条来源关系。"""
    op.create_table(
        "graph_builds",
        sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.id"), primary_key=True),
        sa.Column("index_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(50)),
        sa.Column("token", sa.String(36)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
    )
    op.create_index("ix_graph_builds_status", "graph_builds", ["status"])
    op.create_table(
        "graph_entities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("index_version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False),
    )
    op.create_index("ix_graph_entities_document_id", "graph_entities", ["document_id"])
    op.create_table(
        "graph_facts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("index_version", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("graph_entities.id"), nullable=False),
        sa.Column("target_id", sa.String(36), sa.ForeignKey("graph_entities.id"), nullable=False),
        sa.Column("relation", sa.String(20), nullable=False),
        sa.Column("chunk_id", sa.String(36), sa.ForeignKey("document_chunks.id"), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
    )
    for column in ("document_id", "source_id", "target_id", "chunk_id"):
        op.create_index("ix_graph_facts_" + column, "graph_facts", [column])


def downgrade() -> None:
    """先移除关系再移除实体，保留原有 RAG 数据。"""
    for table in ("graph_facts", "graph_entities", "graph_builds"):
        op.drop_table(table)

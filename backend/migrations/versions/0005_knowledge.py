"""阶段 4 知识、引用与主动记忆表；仅新增表和消息引用字段。"""

import sqlalchemy as sa
from alembic import op

revision = "0005_knowledge"
down_revision = "0004_agent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """应用当前版本的数据库结构变更，供迁移工具顺序执行。"""
    op.create_table(
        "knowledge_bases",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "workspace_id",
            sa.String(length=36),
            sa.ForeignKey("workspaces.id"),
            primary_key=False,
            nullable=False,
        ),
        sa.Column("name", sa.String(length=80), primary_key=False, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), primary_key=False, nullable=False),
    )
    op.create_index("ix_knowledge_bases_workspace_id", "knowledge_bases", ["workspace_id"])
    op.create_table(
        "documents",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "knowledge_base_id",
            sa.String(length=36),
            sa.ForeignKey("knowledge_bases.id"),
            primary_key=False,
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            sa.String(length=36),
            sa.ForeignKey("workspaces.id"),
            primary_key=False,
            nullable=False,
        ),
        sa.Column("filename", sa.String(length=160), primary_key=False, nullable=False),
        sa.Column("content_hash", sa.String(length=64), primary_key=False, nullable=False),
        sa.Column("object_key", sa.String(length=200), primary_key=False, nullable=False),
        sa.Column("size", sa.Integer(), primary_key=False, nullable=False),
        sa.Column("status", sa.String(length=16), primary_key=False, nullable=False),
        sa.Column("error_code", sa.String(length=50), primary_key=False, nullable=True),
        sa.Column("index_version", sa.Integer(), primary_key=False, nullable=False),
        sa.Column("embedding_version", sa.String(length=64), primary_key=False, nullable=False),
        sa.Column("chunk_strategy", sa.String(length=40), primary_key=False, nullable=False),
        sa.Column("collection_name", sa.String(length=64), primary_key=False, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), primary_key=False, nullable=False),
        sa.UniqueConstraint("knowledge_base_id", "content_hash", name="uq_document_hash"),
    )
    op.create_index("ix_documents_status", "documents", ["status"])
    op.create_index("ix_documents_knowledge_base_id", "documents", ["knowledge_base_id"])
    op.create_index("ix_documents_workspace_id", "documents", ["workspace_id"])
    op.create_table(
        "document_chunks",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "document_id",
            sa.String(length=36),
            sa.ForeignKey("documents.id"),
            primary_key=False,
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), primary_key=False, nullable=False),
        sa.Column("page", sa.Integer(), primary_key=False, nullable=True),
        sa.Column("content", sa.Text(), primary_key=False, nullable=False),
        sa.Column("index_version", sa.Integer(), primary_key=False, nullable=False),
    )
    op.create_index("ix_document_chunks_document_id", "document_chunks", ["document_id"])
    op.create_table(
        "memory_preferences",
        sa.Column(
            "workspace_id",
            sa.String(length=36),
            sa.ForeignKey("workspaces.id"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), primary_key=False, nullable=False),
    )
    op.create_table(
        "memories",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "workspace_id",
            sa.String(length=36),
            sa.ForeignKey("workspaces.id"),
            primary_key=False,
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id"),
            primary_key=False,
            nullable=False,
        ),
        sa.Column("content", sa.String(length=1000), primary_key=False, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), primary_key=False, nullable=False),
    )
    op.create_index("ix_memories_user_id", "memories", ["user_id"])
    op.create_index("ix_memories_workspace_id", "memories", ["workspace_id"])
    op.create_table(
        "conversation_summaries",
        sa.Column(
            "conversation_id",
            sa.String(length=36),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("upto_sequence", sa.Integer(), primary_key=False, nullable=False),
        sa.Column("content", sa.Text(), primary_key=False, nullable=False),
        sa.Column("source_ids", sa.JSON(), primary_key=False, nullable=False),
        sa.Column("strategy", sa.String(length=40), primary_key=False, nullable=False),
    )
    op.add_column("messages", sa.Column("sources", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    """撤销当前版本的数据库结构变更，供迁移工具显式回退。"""
    op.drop_column("messages", "sources")
    op.drop_table("conversation_summaries")
    op.drop_table("memories")
    op.drop_table("memory_preferences")
    op.drop_table("document_chunks")
    op.drop_table("documents")
    op.drop_table("knowledge_bases")

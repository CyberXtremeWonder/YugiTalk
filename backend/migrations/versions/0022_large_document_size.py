"""文件大小使用 64 位整数字段，以容纳 5 GB JSONL。"""

import sqlalchemy as sa
from alembic import op

revision = "0022_large_document_size"
down_revision = "0021_mcp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保留已有大小值并扩展存储范围。"""
    op.alter_column("documents", "size", existing_type=sa.Integer(), type_=sa.BigInteger())


def downgrade() -> None:
    """仅当所有大小值均在 32 位范围内时允许数据库回退。"""
    op.alter_column("documents", "size", existing_type=sa.BigInteger(), type_=sa.Integer())

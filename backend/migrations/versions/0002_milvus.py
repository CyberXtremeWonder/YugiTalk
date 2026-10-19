"""向量存储切换 Milvus；保留关系数据，拒绝级联删除已有依赖。"""

from alembic import op

revision = "0002_milvus"
down_revision = "0001_vector"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 本阶段尚无业务向量表；若已有依赖，数据库会拒绝删除，不能静默丢数据。
    """应用当前版本的数据库结构变更，供迁移工具顺序执行。"""
    op.execute("DROP EXTENSION IF EXISTS vector")


def downgrade() -> None:
    # 切换后的普通 PostgreSQL 没有旧扩展；回退迁移版本不迁移 Milvus 数据。
    """撤销当前版本的数据库结构变更，供迁移工具显式回退。"""
    op.execute("SELECT 1")

"""初始化向量扩展；业务表由后续阶段的版本化迁移添加。"""

from alembic import op

revision = "0001_vector"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 兼容历史版本：普通 PostgreSQL 没有扩展包时跳过，已有迁移记录不重跑。
    """应用当前版本的数据库结构变更，供迁移工具顺序执行。"""
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') THEN
                CREATE EXTENSION IF NOT EXISTS vector;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    # 不使用 CASCADE，已有业务依赖时由数据库阻止破坏性回退。
    """撤销当前版本的数据库结构变更，供迁移工具显式回退。"""
    op.execute("DROP EXTENSION IF EXISTS vector")

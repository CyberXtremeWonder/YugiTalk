"""为候选记忆加权排序增加用户确认的重要等级。"""

import sqlalchemy as sa
from alembic import op

revision = "0027_memory_importance"
down_revision = "0026_message_uploads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """现有记忆统一保留为普通等级，不根据正文猜测重要性。"""
    with op.batch_alter_table("memories") as batch:
        batch.add_column(sa.Column("importance", sa.Integer(), nullable=False, server_default="3"))
        batch.create_check_constraint("ck_memory_importance", "importance BETWEEN 1 AND 5")


def downgrade() -> None:
    """移除等级，保留原有记忆正文。"""
    with op.batch_alter_table("memories") as batch:
        batch.drop_constraint("ck_memory_importance", type_="check")
        batch.drop_column("importance")

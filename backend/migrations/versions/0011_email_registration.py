"""增加邮箱验证码、管理员用户名与规范邮箱唯一性。"""

import sqlalchemy as sa
from alembic import op

revision = "0011_email_registration"
down_revision = "0010_tool_management"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保留既有用户和会话，冲突邮箱需人工处理，不能自动合并身份。"""
    op.add_column("users", sa.Column("username", sa.String(80), nullable=True))
    op.create_index("uq_users_username", "users", ["username"], unique=True)
    op.add_column(
        "login_sessions",
        sa.Column("development_mode", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("uq_users_email_normalized", "users", [sa.text("lower(email)")], unique=True)
    op.create_table(
        "email_verifications",
        sa.Column("email", sa.String(254), primary_key=True),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delivered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("consumed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    """移除新增身份字段，真实环境先备份，不自动回退数据库。"""
    op.drop_table("email_verifications")
    op.drop_index("uq_users_email_normalized", table_name="users")
    op.drop_column("login_sessions", "development_mode")
    op.drop_index("uq_users_username", table_name="users")
    op.drop_column("users", "username")

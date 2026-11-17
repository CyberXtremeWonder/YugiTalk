"""用户私有远程 MCP 配置；不改变现有工具历史。"""

import sqlalchemy as sa
from alembic import op

revision = "0021_mcp"
down_revision = "0020_graph_batches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_mcp_servers_account_id", "mcp_servers", ["account_id"])
    op.create_table(
        "mcp_server_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("server_id", sa.String(36), sa.ForeignKey("mcp_servers.id"), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
    )
    op.create_index("ix_mcp_server_versions_server_id", "mcp_server_versions", ["server_id"])
    op.create_table(
        "mcp_credentials",
        sa.Column("server_id", sa.String(36), sa.ForeignKey("mcp_servers.id"), primary_key=True),
        sa.Column("encrypted", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("mcp_credentials")
    op.drop_table("mcp_server_versions")
    op.drop_table("mcp_servers")

"""用户审核工具绑定提交者，既有管理员工具保持原有空间可见范围。"""

import sqlalchemy as sa
from alembic import op

revision = "0013_private_tools"
down_revision = "0012_tool_submissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加所有者外键，并将已有已批准提交对应的工具归属提交者。"""
    op.add_column("managed_tools", sa.Column("owner_id", sa.String(36), nullable=True))
    op.create_foreign_key("fk_managed_tool_owner", "managed_tools", "users", ["owner_id"], ["id"])
    op.create_index("ix_managed_tools_owner_id", "managed_tools", ["owner_id"])
    # 审核功能尚未部署；仍覆盖0012已独立使用过的升级场景，不能将用户工具遗留为公共。
    op.execute(
        "UPDATE managed_tools AS t SET owner_id = s.submitter_id "
        "FROM tool_submissions AS s WHERE s.status = 'approved' "
        "AND s.workspace_id = t.workspace_id "
        "AND s.config->>'name' = t.name AND t.builtin = false"
    )


def downgrade() -> None:
    """回退会失去个人工具隔离，不能在有个人工具时直接降级。"""
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT count(*) FROM managed_tools WHERE owner_id IS NOT NULL")):
        raise ValueError("存在个人工具，禁止移除所有者隔离字段")
    op.drop_index("ix_managed_tools_owner_id", table_name="managed_tools")
    op.drop_constraint("fk_managed_tool_owner", "managed_tools", type_="foreignkey")
    op.drop_column("managed_tools", "owner_id")

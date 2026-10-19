"""移除工作空间与成员，保留资源并将归属直接迁移到用户。"""

import sqlalchemy as sa
from alembic import op

revision = "0015_user_ownership"
down_revision = "0014_workspace_deletion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """共享资源归唯一所有者，个人资源保留原用户，冲突与活跃任务拒绝迁移。"""
    db = op.get_bind()
    if db.dialect.name != "postgresql":
        raise RuntimeError("用户归属迁移需要 PostgreSQL，隔离单测使用最新元数据")
    if db.execute(
        sa.text(
            "SELECT count(*) FROM runs WHERE status IN "
            "('queued','running','waiting_input','waiting_approval')"
        )
    ).scalar():
        raise RuntimeError("存在未结束任务，请先停止或完成任务")
    owners = {}
    spaces = db.execute(sa.text("SELECT id FROM workspaces")).scalars().all()
    for space in spaces:
        people = (
            db.execute(
                sa.text("SELECT user_id FROM memberships WHERE workspace_id=:id AND role='owner'"),
                {"id": space},
            )
            .scalars()
            .all()
        )
        if len(people) != 1:
            raise RuntimeError(f"旧空间 {space} 有 {len(people)} 位所有者，需人工确认归属")
        owners[space] = people[0]
    inspector = sa.inspect(db)
    tables = [
        name
        for name in inspector.get_table_names()
        if name not in {"workspaces", "memberships"}
        and any(column["name"] == "workspace_id" for column in inspector.get_columns(name))
    ]
    # 先移除旧外键，整个迁移由数据库事务保护。
    for table in tables:
        for foreign in inspector.get_foreign_keys(table):
            if "workspace_id" in foreign["constrained_columns"]:
                op.drop_constraint(foreign["name"], table, type_="foreignkey")
        op.alter_column(table, "workspace_id", new_column_name="account_id")
    # 同一用户的记忆偏好合并，任一旧偏好关闭则继续关闭。
    preferences = (
        db.execute(
            sa.text(
                "SELECT user_id,bool_and(enabled) AS enabled "
                "FROM memory_preferences GROUP BY user_id"
            )
        )
        .mappings()
        .all()
    )
    db.execute(sa.text("DELETE FROM memory_preferences"))
    for row in preferences:
        db.execute(
            sa.text(
                "INSERT INTO memory_preferences(account_id,user_id,enabled) "
                "VALUES (:user_id,:user_id,:enabled)"
            ),
            dict(row),
        )
    personal = {
        "conversations": "owner_id",
        "runs": "owner_id",
        "tickets": "owner_id",
        "memories": "user_id",
        "tool_submissions": "submitter_id",
        "audit_events": "actor_id",
    }
    for table in tables:
        if table == "memory_preferences":
            continue
        if table in personal:
            column = personal[table]
            # 审计全局事件原本无范围，继续保留空归属。
            condition = " WHERE account_id IS NOT NULL" if table == "audit_events" else ""
            db.execute(sa.text(f'UPDATE "{table}" SET account_id="{column}"{condition}'))
        else:
            for old, user in owners.items():
                db.execute(
                    sa.text(f'UPDATE "{table}" SET account_id=:user WHERE account_id=:old'),
                    {"user": user, "old": old},
                )
        if table == "managed_tools":
            db.execute(
                sa.text("UPDATE managed_tools SET account_id=owner_id WHERE owner_id IS NOT NULL")
            )
            db.execute(
                sa.text("UPDATE managed_tools SET owner_id=account_id WHERE owner_id IS NULL")
            )
        if table == "skills":
            db.execute(sa.text("UPDATE skills SET owner_id=account_id"))
    # 合并后重名必须人工处理，不静默覆盖不可变配置或历史引用。
    for table in ("skills", "managed_tools"):
        conflicts = db.execute(
            sa.text(
                f'SELECT account_id,name FROM "{table}" WHERE NOT deleted '
                'GROUP BY account_id,name HAVING count(*)>1'
            )
        ).all()
        if conflicts:
            raise RuntimeError(f"{table} 存在同一用户重名资源：{conflicts}")
    for table in tables:
        op.create_foreign_key(f"fk_{table}_account_user", table, "users", ["account_id"], ["id"])
    # 队列预算属于用户；正式重新启用额度前重新配置旧工作空间预算。
    db.execute(sa.text("DELETE FROM budgets WHERE key LIKE 'space:%'"))
    op.drop_table("memberships")
    op.drop_table("workspaces")


def downgrade() -> None:
    """旧共享关系无法无损推导，必须通过迁移前备份恢复。"""
    raise RuntimeError("用户归属迁移不支持自动降级，请使用迁移前完整备份恢复")

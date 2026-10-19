"""工作空间成员权限校验与用户所属空间查询。"""

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.workspace.models import Membership, Workspace


def membership(db: Session, workspace_id: str, user_id: str, manage: bool = False) -> Membership:
    """验证空间成员资格，管理操作额外检查所有者或管理员角色。"""
    member = db.get(Membership, (workspace_id, user_id))
    space = db.get(Workspace, workspace_id)
    if member is None or space is None or space.deleted_at is not None:
        raise HTTPException(404)
    if manage and member.role not in {"owner", "admin"}:
        raise HTTPException(403)
    return member


def workspace_list(
    db: Session, user_id: str, limit: int = 100, cursor: str = ""
) -> list[dict[str, str]]:
    """按用户成员关系查询有界空间列表，支持游标分页。"""
    rows = db.execute(
        select(Workspace, Membership)
        .join(Membership)
        .where(Membership.user_id == user_id, Workspace.id > cursor, Workspace.deleted_at.is_(None))
        .order_by(Workspace.id)
        .limit(limit)
    )
    return [{"id": space.id, "name": space.name, "role": member.role} for space, member in rows]

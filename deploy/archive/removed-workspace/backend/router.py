"""工作空间及成员管理，保护最后一位所有者。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.audit.service import audit
from app.core.entities import now
from app.identity.dependencies import DB, Actor, Writer
from app.identity.models import User
from app.tasks.access import ACTIVE
from app.tasks.models import Run
from app.workspace.access import membership, workspace_list
from app.workspace.contracts import MemberInput, RoleInput, WorkspaceInput
from app.workspace.models import Membership, Workspace

router = APIRouter(prefix="/api/v1", tags=["工作空间"])


@router.delete("/workspaces/{workspace_id}", status_code=204)
def delete_workspace(workspace_id: UUID, db: DB, actor: Writer) -> None:
    """仅所有者可软删除无未结束任务的空间，保留审计与底层资料。"""
    key = str(workspace_id)
    space = db.scalar(select(Workspace).where(Workspace.id == key).with_for_update())
    caller = membership(db, key, actor.user.id)
    if caller.role != "owner":
        raise HTTPException(403)
    if db.scalar(select(Run.id).where(Run.workspace_id == key, Run.status.in_(ACTIVE)).limit(1)):
        raise HTTPException(409, detail={"code": "workspace_active_tasks"})
    if space is None:
        raise HTTPException(404)
    space.deleted_at = now()
    # 同步撤销成员关系，工具和后台检索的直接成员检查也不能再通过。
    db.execute(delete(Membership).where(Membership.workspace_id == key))
    audit(db, actor.user, "workspace.delete", key)
    db.commit()


@router.get("/workspaces")
def list_workspaces(
    db: DB,
    actor: Actor,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: UUID | None = None,
) -> dict[str, object]:
    """分页返回当前用户的成员空间与角色。"""
    items = workspace_list(db, actor.user.id, limit + 1, str(cursor) if cursor else "")
    return {
        "items": items[:limit],
        "next_cursor": items[limit - 1]["id"] if len(items) > limit else None,
    }


@router.post("/workspaces", status_code=201)
def create_workspace(data: WorkspaceInput, db: DB, actor: Writer) -> dict[str, str]:
    """创建新空间并将当前用户设为所有者。"""
    space = Workspace(name=data.name)
    db.add(space)
    db.flush()
    db.add(Membership(workspace_id=space.id, user_id=actor.user.id, role="owner"))
    audit(db, actor.user, "workspace.create", space.id)
    db.commit()
    return {"id": space.id, "name": space.name, "role": "owner"}


@router.get("/workspaces/{workspace_id}")
def get_workspace(workspace_id: UUID, db: DB, actor: Actor) -> dict[str, str]:
    """验证成员资格后读取空间名称与当前角色。"""
    member = membership(db, str(workspace_id), actor.user.id)
    space = db.get(Workspace, str(workspace_id))
    if space is None:
        raise HTTPException(404)
    return {"id": space.id, "name": space.name, "role": member.role}


@router.get("/workspaces/{workspace_id}/members")
def list_members(
    workspace_id: UUID,
    db: DB,
    actor: Actor,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: UUID | None = None,
) -> dict[str, object]:
    """仅允许空间管理员分页查看成员及角色。"""
    membership(db, str(workspace_id), actor.user.id, manage=True)
    rows = db.execute(
        select(User, Membership)
        .join(Membership)
        .where(
            Membership.workspace_id == str(workspace_id), User.id > (str(cursor) if cursor else "")
        )
        .order_by(User.id)
        .limit(limit + 1)
    ).all()
    return {
        "items": [
            {"id": user.id, "email": user.email, "name": user.name, "role": member.role}
            for user, member in rows[:limit]
        ],
        "next_cursor": rows[limit - 1][0].id if len(rows) > limit else None,
    }


@router.post("/workspaces/{workspace_id}/members", status_code=201)
def add_member(workspace_id: UUID, data: MemberInput, db: DB, actor: Writer) -> dict[str, str]:
    """锁定空间并检查账号和已有关系，避免重复加入或覆盖角色。"""
    key = str(workspace_id)
    db.scalar(select(Workspace).where(Workspace.id == key).with_for_update())
    membership(db, key, actor.user.id, manage=True)
    user = db.scalar(
        select(User).where(User.email == data.email.strip().lower(), User.active.is_(True))
    )
    if user is None:
        raise HTTPException(404, detail={"code": "member_user_unavailable"})
    if db.get(Membership, (key, user.id)):
        raise HTTPException(409, detail={"code": "member_already_exists"})
    db.add(Membership(workspace_id=key, user_id=user.id, role=data.role))
    audit(db, actor.user, "membership.add", key, user.id)
    db.commit()
    return {"id": user.id, "role": data.role}


def protect_owner(db: Session, space: str, target: Membership, caller: Membership) -> None:
    """保护所有者权限，阻止删除或降级最后一位所有者。"""
    if target.role == "owner" and caller.role != "owner":
        raise HTTPException(403)
    if target.role == "owner":
        owners = db.scalars(
            select(Membership).where(Membership.workspace_id == space, Membership.role == "owner")
        ).all()
        if len(owners) <= 1:
            raise HTTPException(409, detail={"code": "last_workspace_owner"})


@router.patch("/workspaces/{workspace_id}/members/{user_id}")
def update_member(
    workspace_id: UUID, user_id: UUID, data: RoleInput, db: DB, actor: Writer
) -> dict[str, str]:
    """验证管理与所有者权限后更新成员角色。"""
    key = str(workspace_id)
    db.scalar(select(Workspace).where(Workspace.id == key).with_for_update())
    caller = membership(db, key, actor.user.id, manage=True)
    target = membership(db, key, str(user_id))
    if data.role == "owner" and caller.role != "owner":
        raise HTTPException(403)
    if data.role != target.role:
        protect_owner(db, key, target, caller)
        target.role = data.role
        audit(db, actor.user, "membership.role", key, target.user_id)
        db.commit()
    return {"id": target.user_id, "role": target.role}


@router.delete("/workspaces/{workspace_id}/members/{user_id}", status_code=204)
def remove_member(workspace_id: UUID, user_id: UUID, db: DB, actor: Writer) -> None:
    """在保护最后一位所有者的前提下撤销成员关系。"""
    key = str(workspace_id)
    db.scalar(select(Workspace).where(Workspace.id == key).with_for_update())
    caller = membership(db, key, actor.user.id, manage=True)
    target = membership(db, key, str(user_id))
    protect_owner(db, key, target, caller)
    audit(db, actor.user, "membership.remove", key, target.user_id)
    db.delete(target)
    db.commit()

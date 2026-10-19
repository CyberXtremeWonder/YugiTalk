"""用户主动记忆与偏好接口；操作前实时校验空间权限及内容归属。"""

import re
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from app.accounts.access import AccountId, membership
from app.audit.service import audit
from app.identity.dependencies import DB, Actor, Writer
from app.identity.models import User
from app.memory.models import Memory, MemoryPreference

router = APIRouter(prefix="/api/v1", tags=["偏好记忆"])


class MemoryInput(BaseModel):
    """限定记忆内容及用户确认标记的输入格式。"""

    content: str = Field(min_length=1, max_length=1000)
    confirmed: bool = False


class PreferenceInput(BaseModel):
    """定义长期记忆开关的输入契约。"""

    enabled: bool


def preference(db: DB, workspace: str, user: str) -> MemoryPreference | None:
    """读取当前用户的记忆设置，未设置时返回空值供调用方处理。"""
    return db.get(MemoryPreference, (workspace, user))


@router.get("/me/memories")
def memories(account_id: AccountId, db: DB, actor: Actor) -> dict[str, Any]:
    """在权限边界内返回当前用户的偏好及长期记忆列表。"""
    key = str(account_id)
    membership(db, key, actor.user.id)
    choice = preference(db, key, actor.user.id)
    enabled = choice is None or choice.enabled
    rows = (
        db.scalars(
            select(Memory)
            .where(Memory.account_id == key, Memory.user_id == actor.user.id)
            .order_by(Memory.created_at)
            .limit(20)
        ).all()
        if enabled
        else []
    )
    return {"enabled": enabled, "items": [{"id": row.id, "content": row.content} for row in rows]}


@router.patch("/me/memory-preference")
def toggle_memory(
    account_id: AccountId, data: PreferenceInput, db: DB, actor: Writer
) -> dict[str, Any]:
    """更新长期记忆开关，并记录对应审计操作。"""
    key = str(account_id)
    membership(db, key, actor.user.id)
    # 空间父行串行化开关和偏好写入，避免关闭与保存竞争。
    db.scalar(select(User).where(User.id == key).with_for_update())
    row = preference(db, key, actor.user.id)
    if row is None:
        row = MemoryPreference(account_id=key, user_id=actor.user.id)
        db.add(row)
    row.enabled = data.enabled
    audit(db, actor.user, "memory.toggle", key, actor.user.id)
    db.commit()
    return {"enabled": row.enabled}


def validate_memory(data: MemoryInput) -> str:
    """验证主动确认及记忆内容，拒绝令牌和密码等敏感信息。"""
    text = data.content.strip()
    if (
        not data.confirmed
        or not text
        or re.search(
            r"密码|口令|密钥|令牌|password|secret|token|api[_ -]?key|sk-[a-zA-Z0-9]|Bearer\s",
            text,
            re.I,
        )
    ):
        raise HTTPException(422, detail={"code": "memory_content_invalid"})
    return text


@router.post("/me/memories", status_code=201)
def save_memory(account_id: AccountId, data: MemoryInput, db: DB, actor: Writer) -> dict[str, Any]:
    """检查记忆开关和确认内容后保存用户偏好。"""
    key = str(account_id)
    membership(db, key, actor.user.id)
    db.scalar(select(User).where(User.id == key).with_for_update())
    choice = preference(db, key, actor.user.id)
    if choice and not choice.enabled:
        raise HTTPException(409, detail={"code": "memory_disabled"})
    count = (
        db.scalar(
            select(func.count())
            .select_from(Memory)
            .where(Memory.account_id == key, Memory.user_id == actor.user.id)
        )
        or 0
    )
    if count >= 20:
        raise HTTPException(409)
    row = Memory(account_id=key, user_id=actor.user.id, content=validate_memory(data))
    db.add(row)
    db.flush()
    audit(db, actor.user, "memory.create", key, row.id)
    db.commit()
    return {"id": row.id, "content": row.content}


@router.patch("/memories/{memory_id}")
def edit_memory(memory_id: UUID, data: MemoryInput, db: DB, actor: Writer) -> dict[str, Any]:
    """验证记忆归属和当前开关后更新偏好内容。"""
    row = db.get(Memory, str(memory_id))
    if row is None or row.user_id != actor.user.id:
        raise HTTPException(404)
    membership(db, row.account_id, actor.user.id)
    db.scalar(select(User).where(User.id == row.account_id).with_for_update())
    choice = preference(db, row.account_id, actor.user.id)
    if choice and not choice.enabled:
        raise HTTPException(409, detail={"code": "memory_disabled"})
    row.content = validate_memory(data)
    audit(db, actor.user, "memory.update", row.account_id, row.id)
    db.commit()
    return {"id": row.id, "content": row.content}


@router.delete("/memories/{memory_id}", status_code=204)
def delete_memory(memory_id: UUID, db: DB, actor: Writer) -> None:
    """验证归属后删除指定长期记忆并记录审计。"""
    row = db.get(Memory, str(memory_id))
    if row is None or row.user_id != actor.user.id:
        raise HTTPException(404)
    membership(db, row.account_id, actor.user.id)
    db.execute(delete(Memory).where(Memory.id == row.id))
    audit(db, actor.user, "memory.delete", row.account_id, row.id)
    db.commit()

"""所有资源直接归登录用户，不再提供工作空间与成员共享授权。"""

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from app.identity.dependencies import Actor
from app.identity.models import User


@dataclass
class AccountAccess:
    """兼容工具角色契约，用户对自己的资源拥有管理权。"""

    role: str = "owner"


def membership(db: Session, account_id: str, user_id: str, manage: bool = False) -> AccountAccess:
    """验证资源归属必须等于当前有效用户，拒绝其他用户的数据访问。"""
    user = db.get(User, user_id)
    if account_id != user_id or user is None or not user.active:
        raise HTTPException(404)
    return AccountAccess()


def current_account(actor: Actor) -> UUID:
    """从已验证会话推导归属，不接受客户端提交其他账号范围。"""
    return UUID(actor.user.id)


AccountId = Annotated[UUID, Depends(current_account)]

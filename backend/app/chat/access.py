"""私有对话所有权与空间成员权限校验。"""

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.accounts.access import membership
from app.chat.models import Conversation
from app.identity.dependencies import Principal


def owned_conversation(db: Session, key: str, actor: Principal) -> Conversation:
    """同时验证对话所有权和当前空间成员资格。"""
    conversation = db.get(Conversation, key)
    if conversation is None or conversation.owner_id != actor.user.id:
        raise HTTPException(404)
    membership(db, conversation.account_id, actor.user.id)
    return conversation

"""私有对话创建、编辑、删除与消息历史。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import delete, select

from app.accounts.access import AccountId, membership
from app.audit.service import audit
from app.chat.access import owned_conversation
from app.chat.contracts import Title
from app.chat.models import Conversation, Message
from app.core.pagination import Limit, page
from app.identity.dependencies import DB, Actor, Writer
from app.tasks.access import ACTIVE
from app.tasks.models import Run, RunEvent

router = APIRouter(prefix="/api/v1", tags=["对话"])


@router.get("/me/conversations")
def list_conversations(
    account_id: AccountId, db: DB, actor: Actor, limit: Limit = 20, cursor: UUID | None = None
) -> dict[str, object]:
    """分页列出当前用户在指定空间内的私有对话。"""
    key = str(account_id)
    membership(db, key, actor.user.id)
    rows = db.scalars(
        select(Conversation)
        .where(
            Conversation.account_id == key,
            Conversation.owner_id == actor.user.id,
            Conversation.id > (str(cursor) if cursor else ""),
        )
        .order_by(Conversation.id)
        .limit(limit + 1)
    )
    return page([{"id": row.id, "title": row.title} for row in rows], limit)


@router.post("/me/conversations", status_code=201)
def create_conversation(
    account_id: AccountId, data: Title, db: DB, actor: Writer
) -> dict[str, str]:
    """校验成员资格后为当前用户创建私有对话。"""
    membership(db, str(account_id), actor.user.id)
    row = Conversation(account_id=str(account_id), owner_id=actor.user.id, title=data.title)
    db.add(row)
    db.commit()
    return {"id": row.id, "title": row.title}


@router.patch("/conversations/{conversation_id}")
def rename_conversation(
    conversation_id: UUID, data: Title, db: DB, actor: Writer
) -> dict[str, str]:
    """验证所有权后修改私有对话标题。"""
    row = owned_conversation(db, str(conversation_id), actor)
    row.title = data.title
    db.commit()
    return {"id": row.id, "title": row.title}


@router.delete("/conversations/{conversation_id}", status_code=204)
def remove_conversation(conversation_id: UUID, db: DB, actor: Writer) -> None:
    # 与任务创建争用同一会话行，避免删除与新任务并发产生悬空引用。
    """锁定对话并拒绝删除活动任务，随后清理关联历史。"""
    key = str(conversation_id)
    db.scalar(select(Conversation).where(Conversation.id == key).with_for_update())
    db.expire_all()
    row = owned_conversation(db, key, actor)
    if db.scalar(select(Run.id).where(Run.conversation_id == key, Run.status.in_(ACTIVE)).limit(1)):
        raise HTTPException(409)
    run_ids = select(Run.id).where(Run.conversation_id == key)
    # 活动任务已在上面拦截；按子记录到父记录的顺序清理，防止关联数据残留。
    from app.observability.trace_models import TraceStep

    db.execute(delete(TraceStep).where(TraceStep.run_id.in_(run_ids)))
    db.execute(delete(RunEvent).where(RunEvent.run_id.in_(run_ids)))
    db.execute(delete(Run).where(Run.conversation_id == key))
    db.execute(delete(Message).where(Message.conversation_id == key))
    db.delete(row)
    audit(db, actor.user, "conversation.delete", row.account_id, key)
    db.commit()


@router.get("/conversations/{conversation_id}/messages")
def list_messages(
    conversation_id: UUID,
    db: DB,
    actor: Actor,
    limit: Limit = 20,
    cursor: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    """验证对话所有权后按消息序号分页读取历史。"""
    key = str(conversation_id)
    owned_conversation(db, key, actor)
    # 消息按会话内序号续页，比按时间翻页更明确；多取一条只用于判断是否还有下一页。
    rows = db.scalars(
        select(Message)
        .where(Message.conversation_id == key, Message.sequence > cursor)
        .order_by(Message.sequence)
        .limit(limit + 1)
    ).all()
    return {
        "items": [
            {
                "id": row.id,
                "sequence": row.sequence,
                "role": row.role,
                "content": row.content,
                "sources": row.sources,
            }
            for row in rows[:limit]
        ],
        "next_cursor": rows[limit - 1].sequence if len(rows) > limit else None,
    }

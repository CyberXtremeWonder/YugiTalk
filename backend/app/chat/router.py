"""私有对话创建、编辑、删除与消息历史。"""

import json
import re
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import delete, func, select

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


@router.get("/conversations/{conversation_id}/export")
def export_conversation(
    conversation_id: UUID, db: DB, actor: Actor,
    format: Literal["pdf", "docx", "txt", "json"] = "pdf",
) -> Response:
    """导出本人对话的全部已保存文本，不调用模型或读取其他业务资源。"""
    conversation = owned_conversation(db, str(conversation_id), actor)
    messages = list(db.scalars(select(Message).where(
        Message.conversation_id == conversation.id,
    ).order_by(Message.sequence)))
    media_types = {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "txt": "text/plain; charset=utf-8", "json": "application/json",
    }
    if format == "json":
        content = json.dumps({
            "id": conversation.id, "title": conversation.title,
            "created_at": conversation.created_at.isoformat(),
            "messages": [{"id": message.id, "sequence": message.sequence, "role": message.role,
                          "content": message.content, "created_at": message.created_at.isoformat()}
                         for message in messages],
        }, ensure_ascii=False, indent=2).encode("utf-8")
    else:
        roles = {"user": "用户", "assistant": "助手", "system": "系统"}
        text = conversation.title + "\n\n" + "\n\n".join(
            f"{roles.get(message.role, message.role)}\n{message.content}" for message in messages
        )
        if format == "txt":
            content = text.encode("utf-8")
        else:
            from app.research.files import export_report

            content = export_report(text, format)
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", conversation.title).strip(" .") or "对话"
    return Response(content, media_type=media_types[format], headers={
        "Content-Disposition": f"attachment; filename=conversation.{format}; "
                               f"filename*=UTF-8''{quote(name, safe='')}.{format}",
        "Cache-Control": "no-store",
    })


@router.get("/me/conversations")
def list_conversations(
    account_id: AccountId, db: DB, actor: Actor, limit: Limit = 20, cursor: UUID | None = None
) -> dict[str, object]:
    """分页列出当前用户在指定空间内的私有对话。"""
    key = str(account_id)
    membership(db, key, actor.user.id)
    activity = select(
        Message.conversation_id, func.max(Message.created_at).label("last_message_at"),
    ).join(Conversation, Conversation.id == Message.conversation_id).where(
        Conversation.account_id == key,
        Conversation.owner_id == actor.user.id,
    ).group_by(Message.conversation_id).subquery()
    updated = func.coalesce(activity.c.last_message_at, Conversation.created_at)
    query = (
        select(Conversation, updated.label("updated_at"))
        .outerjoin(activity, activity.c.conversation_id == Conversation.id)
        .where(
            Conversation.account_id == key,
            Conversation.owner_id == actor.user.id,
        )
    )
    if cursor:
        boundary = db.execute(query.where(Conversation.id == str(cursor))).first()
        if boundary is None:
            raise HTTPException(404)
        query = query.where(
            (updated < boundary.updated_at)
            | ((updated == boundary.updated_at) & (Conversation.id < str(cursor)))
        )
    rows = db.execute(query.order_by(updated.desc(), Conversation.id.desc()).limit(limit + 1))
    return page([{
        "id": row.Conversation.id, "title": row.Conversation.title,
        "updated_at": row.updated_at.isoformat(),
    } for row in rows], limit)


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
    from app.research.router import attachments

    runs = list(db.scalars(select(Run).where(Run.conversation_id == key)))
    origins = {r.snapshot.get("conversation_upto_sequence"): r.id for r in runs}

    return {
        "items": [
            {
                "id": row.id,
                "sequence": row.sequence,
                "run_id": row.id if row.role == "assistant" else origins.get(row.sequence),
                "role": row.role,
                "content": row.content,
                "uploaded_files": [
                    {key: file[key] for key in ("filename", "size", "truncated")}
                    for file in row.uploaded_files
                ] if row.role == "user" else [],
                "sources": row.sources,
                "attachments": attachments(db, row.id, actor.user.id)
                if row.role == "assistant" else [],
            }
            for row in rows[:limit]
        ],
        "next_cursor": rows[limit - 1].sequence if len(rows) > limit else None,
    }

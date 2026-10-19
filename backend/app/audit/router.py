"""平台审计元数据查询，管理员权限不授予空间资料访问权。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import and_, or_, select

from app.audit.models import AuditEvent
from app.identity.dependencies import DB, Actor

router = APIRouter(prefix="/api/v1", tags=["审计"])


@router.get("/admin/audit")
def platform_audit(
    db: DB,
    actor: Actor,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: UUID | None = None,
) -> dict[str, object]:
    # 平台管理员仅访问审计元数据，不因此绕过任何工作空间成员校验。
    """仅允许平台管理员按游标查询审计元数据。"""
    if not actor.user.platform_admin:
        raise HTTPException(403)
    query = select(AuditEvent)
    if cursor:
        previous = db.get(AuditEvent, str(cursor))
        if previous is None:
            raise HTTPException(404)
        query = query.where(
            or_(
                AuditEvent.created_at < previous.created_at,
                and_(AuditEvent.created_at == previous.created_at, AuditEvent.id < previous.id),
            )
        )
    rows = db.scalars(
        query.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(limit + 1)
    ).all()
    return {
        "items": [
            {
                "id": item.id,
                "actor_id": item.actor_id,
                "account_id": item.account_id,
                "action": item.action,
                "target_id": item.target_id,
                "created_at": item.created_at.isoformat(),
            }
            for item in rows[:limit]
        ],
        "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
    }

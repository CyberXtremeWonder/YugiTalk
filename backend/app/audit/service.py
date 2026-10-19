"""在调用方事务内记录审计事件，避免审计与业务状态分离提交。"""

from sqlalchemy.orm import Session

from app.audit.models import AuditEvent
from app.identity.models import User


def audit(
    db: Session,
    actor: User,
    action: str,
    account_id: str | None = None,
    target_id: str | None = None,
) -> None:
    """在业务事务中追加审计记录，由调用方统一提交。"""
    db.add(AuditEvent(actor_id=actor.id, action=action, account_id=account_id, target_id=target_id))

"""只读盘点身份记录，并以固定截止点小批量清理已失效数据。"""

from datetime import UTC, datetime
from typing import cast

from sqlalchemy import Engine, case, delete, func, select, text
from sqlalchemy.orm import Session

from app.core.entities import now
from app.identity.models import EmailVerification, LoginSession

DEFAULT_BATCH_SIZE = 500
MAX_BATCH_SIZE = 1000


def utc_cutoff(cutoff: datetime | None) -> datetime:
    """统一使用带时区的UTC截止点，拒绝无法确定时区的输入。"""
    value = cutoff if cutoff is not None else now()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("cutoff必须包含时区")
    return value.astimezone(UTC)


def inspect_expired(db: Session, cutoff: datetime) -> dict[str, dict[str, int | str | None]]:
    """每表一个聚合查询，不读取身份内容、不触发自动写入或提交事务。"""
    fixed = utc_cutoff(cutoff)
    report: dict[str, dict[str, int | str | None]] = {}
    with db.no_autoflush:
        for model in (LoginSession, EmailVerification):
            rows, expired, oldest = cast(
                tuple[int, int, datetime | None],
                db.execute(
                    select(
                        func.count(),
                        func.coalesce(func.sum(case((model.expires_at <= fixed, 1), else_=0)), 0),
                        func.min(model.expires_at),
                    ).select_from(model)
                ).one(),
            )
            if oldest is not None:
                oldest = (
                    oldest.replace(tzinfo=UTC) if oldest.tzinfo is None else oldest.astimezone(UTC)
                )
            report[model.__tablename__] = {
                "rows": int(rows),
                "expired": int(expired),
                "oldest_expires_at": oldest.isoformat() if oldest is not None else None,
            }
    return report


def cleanup_expired(
    engine: Engine, cutoff: datetime | None = None, batch_size: int = DEFAULT_BATCH_SIZE
) -> dict[str, int | str]:
    """每表最多删除一批过期行，候选加锁并复核期限，两表在同一事务提交。"""
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError("batch_size必须在1到1000之间")
    fixed = utc_cutoff(cutoff)
    result: dict[str, int | str] = {
        "cutoff": fixed.isoformat(),
        "login_sessions": 0,
        "email_verifications": 0,
    }
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            # 清理不得长时间占用业务连接；设置仅在本次事务有效。
            connection.execute(text("SET LOCAL statement_timeout = '5s'"))
            connection.execute(text("SET LOCAL lock_timeout = '1s'"))
        for model, key in (
            (LoginSession, LoginSession.token_hash),
            (EmailVerification, EmailVerification.email),
        ):
            keys = list(
                connection.scalars(
                    select(key)
                    .where(model.expires_at <= fixed)
                    .order_by(model.expires_at, key)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            if keys:
                removed = connection.execute(
                    delete(model).where(key.in_(keys), model.expires_at <= fixed)
                )
                result[model.__tablename__] = removed.rowcount
    return result

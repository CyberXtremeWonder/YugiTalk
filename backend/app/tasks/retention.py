"""按用户保留最新 100 条任务，聊天及生成文件独立保留。"""

from datetime import timedelta

from sqlalchemy import Engine, delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.core.entities import now
from app.identity.models import User
from app.observability.trace_models import TraceStep
from app.tasks.access import TERMINAL
from app.tasks.ledger_models import Approval, RunRetry, Ticket, ToolExecution
from app.tasks.models import Run, RunEvent

HISTORY_LIMIT = 100


def prune_user_history(db: Session, owner_id: str) -> int:
    """与提交/恢复/结算共用用户锁；活动任务及尚在收尾的 Trace 暂不清理。"""
    db.scalar(select(User.id).where(User.id == owner_id).with_for_update())
    recent = (
        select(Run.id)
        .where(Run.owner_id == owner_id)
        .order_by(Run.created_at.desc(), Run.id.desc())
        .limit(HISTORY_LIMIT)
    )
    finishing = (
        select(TraceStep.id)
        .where(TraceStep.run_id == Run.id, TraceStep.status == "running")
        .exists()
    )
    keys = list(
        db.scalars(
            select(Run.id)
            .where(
                Run.owner_id == owner_id,
                Run.id.not_in(recent),
                Run.status.in_(TERMINAL),
                or_(
                    ~finishing,
                    Run.finished_at < now() - timedelta(minutes=1),
                    Run.finished_at.is_(None) & (Run.created_at < now() - timedelta(minutes=1)),
                ),
            )
            .with_for_update()
        )
    )
    if not keys:
        return 0
    executions = select(ToolExecution.id).where(ToolExecution.run_id.in_(keys))
    db.execute(update(Ticket).where(Ticket.execution_id.in_(executions)).values(execution_id=None))
    db.execute(delete(Approval).where(Approval.execution_id.in_(executions)))
    db.execute(delete(ToolExecution).where(ToolExecution.run_id.in_(keys)))
    db.execute(delete(RunRetry).where(RunRetry.run_id.in_(keys)))
    db.execute(delete(TraceStep).where(TraceStep.run_id.in_(keys)))
    db.execute(delete(RunEvent).where(RunEvent.run_id.in_(keys)))
    db.execute(delete(Run).where(Run.id.in_(keys)))
    return len(keys)


def prune_history(engine: Engine) -> int:
    """调度器清理已有超额记录，也收尾此前仍活动的旧任务。"""
    with Session(engine) as db:
        owners = list(
            db.scalars(
                select(Run.owner_id)
                .group_by(Run.owner_id)
                .having(func.count(Run.id) > HISTORY_LIMIT)
                .order_by(Run.owner_id)
                .limit(100)
            )
        )
    removed = 0
    for owner in owners:
        with Session(engine) as db:
            removed += prune_user_history(db, owner)
            db.commit()
    return removed

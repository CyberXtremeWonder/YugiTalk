"""管理员任务 Trace 列表与完整详情，只返回执行元数据。"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.core.entities import now
from app.identity.dependencies import DB, Actor
from app.observability.trace_models import TraceStep
from app.observability.traces import usage_summary
from app.tasks.models import Run

router = APIRouter(prefix="/api/v1/admin/traces")


def timestamp(value: datetime) -> datetime:
    """SQLite 测试与实际数据库使用一致 UTC。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def run_info(run: Run) -> dict[str, object]:
    """历史用量与保守额度分开，列表不暴露用户问题正文。"""
    return {
        "id": run.id,
        "status": run.status,
        "model": run.snapshot.get("model"),
        "provider": run.snapshot.get("provider"),
        "created_at": timestamp(run.created_at).isoformat(),
        "finished_at": timestamp(run.finished_at).isoformat() if run.finished_at else None,
        "attempts": run.attempts,
        "charged_tokens": run.charged_tokens,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "error_code": run.error_code,
    }


@router.get("")
def list_traces(
    actor: Actor,
    db: DB,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    cursor: UUID | None = None,
) -> dict[str, object]:
    """按任务创建顺序分页，所有请求重新检查管理员身份。"""
    if not actor.user.platform_admin:
        raise HTTPException(403)
    query = select(Run)
    if cursor:
        boundary = db.get(Run, str(cursor))
        if boundary is None:
            raise HTTPException(404)
        query = query.where(
            (Run.created_at < boundary.created_at)
            | ((Run.created_at == boundary.created_at) & (Run.id < boundary.id))
        )
    rows = list(db.scalars(query.order_by(Run.created_at.desc(), Run.id.desc()).limit(limit + 1)))
    return {
        "items": [run_info(run) for run in rows[:limit]],
        "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
    }


@router.get("/{run_id}")
def trace_detail(run_id: UUID, actor: Actor, db: DB) -> dict[str, object]:
    """返回步骤树所需数据，失效执行中的步骤显示中断而不伪造完成时间。"""
    if not actor.user.platform_admin:
        raise HTTPException(403)
    run = db.get(Run, str(run_id))
    if run is None:
        raise HTTPException(404)
    rows = list(
        db.scalars(
            select(TraceStep)
            .where(TraceStep.run_id == run.id)
            .order_by(TraceStep.started_at, TraceStep.id)
        )
    )
    sampled = now()
    steps = []
    for row in rows:
        interrupted = row.status == "running" and (
            row.attempt < run.attempts
            or run.status not in ("running", "queued")
            or (run.lease_until is not None and timestamp(run.lease_until) <= sampled)
        )
        steps.append(
            {
                "id": row.id,
                "parent_id": row.parent_id,
                "kind": row.kind,
                "name": row.name,
                "attempt": row.attempt,
                "iteration": row.iteration,
                "status": "interrupted" if interrupted else row.status,
                "started_at": timestamp(row.started_at).isoformat(),
                "duration_ms": row.duration_ms,
                "elapsed_ms": row.duration_ms
                if row.duration_ms is not None
                else (
                    None
                    if interrupted
                    else round((sampled - timestamp(row.started_at)).total_seconds() * 1000)
                ),
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "details": row.details,
            }
        )
    return {
        "run": run_info(run),
        "steps": steps,
        "sampled_at": sampled.isoformat(),
        "summary": {
            **usage_summary(rows),
            "recorded": bool(rows),
            "loops": sum(row.kind == "loop" for row in rows),
            "tool_calls": sum(row.kind == "tool" and not row.details.get("reused") for row in rows),
            "tool_reuses": sum(
                row.kind == "tool" and bool(row.details.get("reused")) for row in rows
            ),
            "memory_reads": sum(int(str(row.details.get("memory_reads", 0))) for row in rows),
            "memory_hits": sum(int(str(row.details.get("memory_hits", 0))) for row in rows),
            "retrievals": sum(row.kind == "retrieval" for row in rows),
        },
    }

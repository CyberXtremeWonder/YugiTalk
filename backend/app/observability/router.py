"""内部指标接口使用独立认证，公网反向代理不转发此路径。"""

import hmac

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.entities import now
from app.identity.dependencies import DB, Actor
from app.observability.dashboard import task_snapshot
from app.tasks.models import Run

router = APIRouter()


@router.get("/api/v1/admin/monitoring")
def dashboard(request: Request, actor: Actor, db: DB) -> dict[str, object]:
    """只向平台管理员提供监控元数据，不把运维令牌传给浏览器。"""
    if not actor.user.platform_admin:
        raise HTTPException(403)
    sampled = now()
    return {
        "sampled_at": sampled.isoformat(),
        "window_minutes": 15,
        "http": request.app.state.live_requests.snapshot(sampled),
        "tasks": task_snapshot(db, sampled),
    }


@router.get("/internal/metrics", include_in_schema=False)
def metrics(request: Request) -> Response:
    """认证后返回聚合指标，不通过用户会话暴露跨空间运维数据。"""
    token = request.app.state.settings.metrics_token.get_secret_value()
    supplied = request.headers.get("Authorization", "")
    if not token or not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
        raise HTTPException(401)
    telemetry = request.app.state.telemetry
    with Session(request.app.state.database) as db:
        for status in ("queued", "running", "succeeded", "failed", "cancelled"):
            count = db.scalar(select(func.count()).select_from(Run).where(Run.status == status))
            telemetry.runs.labels(status).set(count or 0)
        # 空间与用户预算分别记账；按任务汇总，避免把同一次消耗统计两遍。
        telemetry.tokens.set(db.scalar(select(func.coalesce(func.sum(Run.charged_tokens), 0))) or 0)
    return Response(telemetry.render(), media_type="text/plain; version=0.0.4; charset=utf-8")

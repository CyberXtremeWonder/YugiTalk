"""独立区分存活和就绪，依赖故障时进程仍可响应诊断请求。"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.health.checks import DependencyChecks, HealthChecker

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """健康契约不暴露内部主机名、凭证或异常堆栈。"""

    status: str
    checks: DependencyChecks = {}


@router.get("/health/live", response_model=HealthResponse)
async def live() -> HealthResponse:
    """仅检查 API 存活，不受模型或存储可用性影响。"""
    return HealthResponse(status="ok")


@router.get(
    "/health/ready", response_model=HealthResponse, responses={503: {"model": HealthResponse}}
)
async def ready(request: Request) -> JSONResponse:
    """就绪检查支持注入，测试可独立验证失败与恢复行为。"""
    checker: HealthChecker = request.app.state.health_checker
    checks = await checker()
    healthy = bool(checks) and all(checks.values())
    body = HealthResponse(status="ready" if healthy else "unavailable", checks=checks)
    return JSONResponse(body.model_dump(), status_code=200 if healthy else 503)

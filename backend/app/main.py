"""FastAPI 应用工厂，配置、生命周期与协议行为在此组装。"""

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from opentelemetry import trace
from sqlalchemy import Engine
from starlette.exceptions import HTTPException
from starlette.responses import Response

from app.agents.router import router as agents_router
from app.audit.router import router as audit_router
from app.chat.router import router as chat_router
from app.core.config import Settings, load_settings
from app.core.database import create_database
from app.core.logging import configure_logging, log_failure
from app.health.checks import DependencyChecker, HealthChecker
from app.health.router import router as health_router
from app.identity.login_limit import LoginLimiter, RedisLoginLimiter
from app.identity.registration import send_code
from app.identity.router import router as identity_router
from app.memory.router import router as memory_router
from app.observability.dashboard import LiveRequests
from app.observability.router import router as observability_router
from app.observability.telemetry import Telemetry, current_trace_id
from app.observability.trace_router import router as trace_router
from app.rag.indexing import dispatch_document
from app.rag.resume import router as rag_resume_router
from app.rag.router import router as knowledge_router
from app.skills.router import router as skills_router
from app.tasks.execution import dispatch_run
from app.tasks.router import router as reliability_router
from app.tools.review_router import router as tool_review_router
from app.tools.router import router as tools_router

logger = logging.getLogger(__name__)


def error_response(request: Request, code: str, message: str, status: int) -> JSONResponse:
    """统一错误结构，输入验证错误不回显敏感原始输入。"""
    request.state.error_code = code
    return JSONResponse(
        {
            "code": code,
            "message": message,
            "request_id": getattr(request.state, "request_id", ""),
            "details": {},
        },
        status_code=status,
    )


def create_app(
    settings: Settings | None = None,
    checker: HealthChecker | None = None,
    engine: Engine | None = None,
    limiter: LoginLimiter | None = None,
) -> FastAPI:
    """默认组装真实依赖，测试可显式注入配置和探测器。"""
    config = settings or load_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """启动日志并在应用关闭时释放数据库连接池。"""
        configure_logging(config.log_level)
        logger.info("API 已启动")
        yield
        app.state.database.dispose()
        app.state.telemetry.shutdown()
        logger.info("API 已完成关闭")

    development = config.app_env != "production"
    app = FastAPI(
        title="YugiTalk API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if development else None,
        redoc_url=None,
        openapi_url="/openapi.json" if development else None,
    )
    app.state.health_checker = checker or DependencyChecker(config).check
    app.state.settings = config
    app.state.telemetry = Telemetry(config, "api")
    app.state.live_requests = LiveRequests()
    app.state.database = engine if engine is not None else create_database(config)
    app.state.login_limiter = limiter if limiter is not None else RedisLoginLimiter(config)
    app.state.registration_limiter = (
        limiter if limiter is not None else RedisLoginLimiter(config, "registration")
    )
    app.state.send_verification_code = send_code
    app.state.dispatch_run = dispatch_run
    app.state.dispatch_document = dispatch_document
    app.include_router(health_router)
    app.include_router(identity_router)
    app.include_router(agents_router)
    app.include_router(chat_router)
    app.include_router(audit_router)
    app.include_router(knowledge_router)
    app.include_router(rag_resume_router)
    app.include_router(memory_router)
    app.include_router(skills_router)
    app.include_router(tools_router)
    app.include_router(tool_review_router)
    app.include_router(reliability_router)
    app.include_router(observability_router)
    app.include_router(trace_router)

    @app.get("/", include_in_schema=False)
    async def index() -> RedirectResponse:
        """后端根入口引导到可用页面，生产环境不暴露开发文档。"""
        return RedirectResponse("/docs" if development else "/health/live")

    if config.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key", "Last-Event-ID"],
            expose_headers=["X-Request-ID", "X-Trace-ID"],
        )

    @app.middleware("http")
    async def request_metadata(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # 不信任外部请求 ID，服务端生成标识，避免日志注入。
        """生成请求标识、记录有界日志并设置安全响应头。"""
        request.state.request_id = uuid4().hex
        started = time.monotonic()
        with app.state.telemetry.span("http.request") as span:
            span.set_attribute("request.id", request.state.request_id)
            trace_id = current_trace_id()
            try:
                response = await call_next(request)
            except Exception as error:
                span.set_attribute("error.type", type(error).__name__)
                log_failure(
                    logger,
                    "http.request",
                    error,
                    request_id=request.state.request_id,
                    trace_id=trace_id,
                    error_code="internal_error",
                )
                response = error_response(request, "internal_error", "服务暂时不可用", 500)
            route = getattr(request.scope.get("route"), "path", "unmatched")
            method = (
                request.method
                if request.method in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}
                else "OTHER"
            )
            span.set_attribute("http.route", route)
            span.set_attribute("http.request.method", method)
            span.set_attribute("http.response.status_code", response.status_code)
            if response.status_code >= 500:
                span.set_status(trace.Status(trace.StatusCode.ERROR))
            app.state.telemetry.requests.labels(method, route, str(response.status_code)).inc()
            app.state.telemetry.duration.labels(method, route).observe(time.monotonic() - started)
            response.headers["X-Trace-ID"] = trace_id
        response.headers["X-Request-ID"] = request.state.request_id
        # 面板轮询不进入业务请求统计，避免没有业务流量时制造请求曲线。
        if route not in {"/api/v1/admin/monitoring", "/internal/metrics"}:
            app.state.live_requests.record(
                method,
                route,
                response.status_code,
                (time.monotonic() - started) * 1000,
                request.state.request_id,
                trace_id,
                getattr(request.state, "error_code", None),
            )
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        log = (
            logger.error
            if response.status_code >= 500
            else (logger.warning if response.status_code >= 400 else logger.info)
        )
        log(
            "请求完成",
            extra={
                "request_id": request.state.request_id,
                "trace_id": response.headers.get("X-Trace-ID", ""),
                "route": getattr(request.scope.get("route"), "path", "unmatched"),
                "method": method,
                "error_code": getattr(request.state, "error_code", None),
                "status_code": response.status_code,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            },
        )
        return response

    @app.exception_handler(HTTPException)
    async def handle_http(request: Request, error: HTTPException) -> JSONResponse:
        """将已知业务错误映射为公开响应，避免泄露内部异常详情。"""
        messages = {
            401: ("authentication_required", "登录信息无效或会话已过期"),
            403: ("permission_denied", "无权执行此操作或请求校验失败"),
            404: ("not_found", "资源不存在"),
            409: ("conflict", "操作与当前状态冲突"),
            429: ("rate_limited", "请求过于频繁，请稍后重试"),
            503: ("dependency_unavailable", "服务依赖暂不可用"),
        }
        code, message = messages.get(error.status_code, ("request_error", "请求无法完成"))
        # 只允许预定义的业务提示，不能将原始异常或任意 detail 返回客户端。
        business_errors = {
            "email_already_registered": (409, "该邮箱已注册，请直接登录"),
            "verification_code_invalid": (422, "验证码无效、已过期或尝试次数过多，请重新获取"),
            "registration_mail_unavailable": (503, "注册邮件服务尚未配置或暂不可用"),
            "registration_disabled": (403, "当前服务未开放注册"),
            "development_admin_required": (403, "开发工具面板仅限管理员访问"),
            "admin_account_protected": (409, "管理员账号不能通过启停面板停用"),
            "tool_submission_limit": (409, "待审工具已达20个，请等待审核"),
            "tool_review_required": (403, "用户工具必须提交管理员审核后发布"),
            "tool_submission_reviewed": (409, "该提交已完成审核，请刷新列表"),
            "tool_submitter_unavailable": (409, "提交者账号已停用，不能批准工具"),
            "document_file_invalid": (
                422,
                "仅支持非空 UTF-8 TXT、Markdown 或文本 PDF，文件名不能包含路径",
            ),
            "knowledge_document_limit": (409, "单个知识库最多支持 200 份有效文档"),
            "memory_content_invalid": (422, "请主动确认偏好，不保存密码、令牌或密钥等敏感内容"),
            "memory_disabled": (409, "长期记忆已关闭，请先开启再保存或修改偏好"),
            "run_budget_insufficient": (
                429,
                "剩余额度不足以预留单次任务预算，请降低助手预算或联系管理员调整总额度",
            ),
            "run_concurrency_exceeded": (
                429,
                "正在执行的任务数量已达上限，请等待完成或停止已有任务",
            ),
            "member_already_exists": (409, "该用户已是当前工作空间成员，无需重复添加"),
            "member_user_unavailable": (404, "用户不存在或已停用，请先初始化账号"),
            "last_workspace_owner": (409, "不能移除或降级工作空间的最后一位所有者"),
            "workspace_active_tasks": (409, "空间内有未结束任务，请先停止或完成任务后再删除"),
            "model_configuration_changed": (409, "模型配置已改变，请重新发布助手版本"),
            "skill_unavailable": (409, "技能已停用或删除，请重新选择"),
            "skill_content_changed": (409, "技能版本校验失败，请重新选择或联系管理员"),
            "skill_name_conflict": (409, "当前空间已存在同名技能"),
            "skill_catalog_limit": (409, "当前空间最多支持 100 个有效技能"),
            "skill_version_limit": (409, "单个技能最多发布 100 个版本"),
        }
        business_code = error.detail.get("code") if isinstance(error.detail, dict) else None
        if isinstance(business_code, str) and business_code in business_errors:
            status, business_message = business_errors[business_code]
            if status == error.status_code:
                code, message = business_code, business_message
        response = error_response(request, code, message, error.status_code)
        if error.headers:
            response.headers.update(error.headers)
        return response

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, _: RequestValidationError) -> JSONResponse:
        """返回统一参数验证错误，禁止回显敏感输入。"""
        return error_response(request, "validation_error", "请求参数不符合约定", 422)

    return app

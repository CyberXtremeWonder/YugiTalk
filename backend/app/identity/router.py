"""登录、退出与当前会话接口。"""

import secrets
from datetime import timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.audit.service import audit
from app.core.config import Settings
from app.core.entities import now
from app.identity.contracts import AccountStateInput, EmailInput, LoginInput, RegisterInput
from app.identity.dependencies import COOKIE, DB, Actor, Principal, Writer, origin_guard
from app.identity.login_limit import LoginLimiter
from app.identity.models import LoginSession, User
from app.identity.registration import issue_code, mail_ready, register_user
from app.identity.security import digest, dummy_hash, hasher, verify_password

router = APIRouter(prefix="/api/v1", tags=["身份认证"])


@router.post("/auth/development-mode")
def enable_development_mode(db: DB, actor: Writer) -> dict[str, bool]:
    """允许已登录的平台管理员显式开启当前会话的管理工具面板。"""
    if not actor.user.platform_admin:
        raise HTTPException(403, detail={"code": "development_admin_required"})
    actor.session.development_mode = True
    audit(db, actor.user, "session.development_mode", None)
    db.commit()
    return {"development_mode": True}


def identity(request: Request, db: Session, actor: Principal) -> dict[str, object]:
    """返回当前用户、所属空间及仅保存在客户端内存的防伪令牌。"""
    settings: Settings = request.app.state.settings
    return {
        "user": {
            "id": actor.user.id,
            "email": actor.user.email,
            "name": actor.user.name,
            "platform_admin": actor.user.platform_admin,
        },
        "csrf_token": digest(settings.session_secret.get_secret_value(), "csrf", actor.token),
        "development_mode": actor.session.development_mode and actor.user.platform_admin,
    }


@router.post("/auth/login")
def login(data: LoginInput, request: Request, response: Response, db: DB) -> dict[str, object]:
    """验证凭证并轮换会话令牌，使用安全选项写入浏览器会话。"""
    origin_guard(request)
    limiter: LoginLimiter = request.app.state.login_limiter
    limiter(request.client.host if request.client else "unknown", data.email)
    user = db.scalar(
        select(User).where(or_(func.lower(User.email) == data.email, User.username == data.email))
    )
    # 未找到账号也执行一次密码哈希校验，减少通过响应耗时探测账号是否存在的差异。
    valid = verify_password(user.password_hash if user else dummy_hash, data.password)
    if user is None or not user.active or not valid:
        raise HTTPException(401)
    if data.development_mode and not user.platform_admin:
        raise HTTPException(403, detail={"code": "development_admin_required"})
    settings: Settings = request.app.state.settings
    if hasher.check_needs_rehash(user.password_hash):
        # 登录成功时顺便升级旧哈希参数，无需保存或找回原始明文密码。
        user.password_hash = hasher.hash(data.password)
    # 每次登录轮换随机令牌，旧 Cookie 对应的会话立即撤销。
    old = request.cookies.get(COOKIE)
    if old:
        db.execute(
            delete(LoginSession).where(
                LoginSession.token_hash
                == digest(settings.session_secret.get_secret_value(), "session", old)
            )
        )
    db.execute(
        delete(LoginSession).where(
            LoginSession.user_id == user.id, LoginSession.expires_at <= now()
        )
    )
    token = secrets.token_urlsafe(32)
    session = LoginSession(
        token_hash=digest(settings.session_secret.get_secret_value(), "session", token),
        user_id=user.id,
        expires_at=now() + timedelta(seconds=settings.session_ttl_seconds),
        development_mode=data.development_mode,
    )
    db.add(session)
    audit(db, user, "auth.login")
    db.commit()
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        secure=settings.app_env == "production",
        samesite="lax",
        path="/",
        max_age=settings.session_ttl_seconds,
    )
    response.headers["Cache-Control"] = "no-store"
    return identity(request, db, Principal(user, session, token))


@router.get("/auth/options")
def options(request: Request) -> dict[str, bool]:
    """只公开注册能力与邮件就绪状态，不公开邮件账号或管理员凭证。"""
    settings: Settings = request.app.state.settings
    return {
        "registration_enabled": settings.registration_enabled,
        "mail_ready": mail_ready(settings),
    }


@router.post("/auth/registration-code")
def registration_code(data: EmailInput, request: Request, db: DB) -> dict[str, int | str]:
    """经来源校验发送验证码，按用户要求对已注册邮箱返回明确的冲突提示。"""
    origin_guard(request)
    issue_code(request, db, data.email)
    return {"message": "如果该邮箱可以注册，验证码将发送到邮箱", "retry_after": 60}


@router.post("/auth/register", status_code=201)
def register(data: RegisterInput, request: Request, db: DB) -> dict[str, str]:
    """验证邮箱后创建普通账号和个人空间，注册成功后使用登录流程建立会话。"""
    origin_guard(request)
    register_user(request, db, data)
    return {"status": "registered"}


@router.get("/admin/development/status")
async def development_status(request: Request, actor: Actor) -> dict[str, object]:
    """逐请求验证当前管理员权限与会话模式，只返回运行摘要，不暴露密钥或修改环境。"""
    if not actor.user.platform_admin or not actor.session.development_mode:
        raise HTTPException(403, detail={"code": "development_admin_required"})
    settings: Settings = request.app.state.settings
    checks = await request.app.state.health_checker()
    return {
        "environment": settings.app_env,
        "model_provider": settings.model_provider,
        "model_id": settings.model_id,
        "embedding_model": settings.embedding_model,
        "registration_ready": settings.registration_enabled and mail_ready(settings),
        "checks": checks,
    }


def development_admin(actor: Principal) -> None:
    """管理操作同时要求当前平台管理员身份和服务端开发模式会话。"""
    if not actor.user.platform_admin or not actor.session.development_mode:
        raise HTTPException(403, detail={"code": "development_admin_required"})


@router.get("/admin/users")
def admin_users(
    db: DB,
    actor: Actor,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: UUID | None = None,
) -> dict[str, object]:
    """有界分页读取账号基本状态，不返回密码摘要、验证码或会话令牌。"""
    development_admin(actor)
    rows = db.scalars(
        select(User)
        .where(User.id > (str(cursor) if cursor else ""))
        .order_by(User.id)
        .limit(limit + 1)
    ).all()
    return {
        "items": [
            {
                "id": user.id,
                "name": user.name,
                "email": user.email,
                "active": user.active,
                "platform_admin": user.platform_admin,
            }
            for user in rows[:limit]
        ],
        "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
    }


@router.patch("/admin/users/{user_id}")
def admin_account_state(
    user_id: UUID, data: AccountStateInput, db: DB, actor: Writer
) -> dict[str, bool]:
    """启停普通账号并撤销停用账号会话，管理员账号需独立管理，避免锁死管理入口。"""
    development_admin(actor)
    target = db.get(User, str(user_id))
    if target is None:
        raise HTTPException(404)
    if target.platform_admin or target.id == actor.user.id:
        raise HTTPException(409, detail={"code": "admin_account_protected"})
    target.active = data.active
    # 停用同时撤销会话，重新启用后也必须重新登录，旧会话不会复活。
    if not data.active:
        db.execute(delete(LoginSession).where(LoginSession.user_id == target.id))
    audit(
        db,
        actor.user,
        "admin.account.enable" if data.active else "admin.account.disable",
        target_id=target.id,
    )
    db.commit()
    return {"active": target.active}


@router.get("/auth/me")
def me(request: Request, response: Response, db: DB, actor: Actor) -> dict[str, object]:
    """校验当前会话并返回身份信息，禁止缓存身份响应。"""
    response.headers["Cache-Control"] = "no-store"
    return identity(request, db, actor)


@router.post("/auth/logout", status_code=204)
def logout(response: Response, request: Request, db: DB, actor: Writer) -> None:
    """撤销数据库会话并删除浏览器中的会话凭证。"""
    db.delete(actor.session)
    audit(db, actor.user, "auth.logout")
    db.commit()
    settings: Settings = request.app.state.settings
    response.delete_cookie(
        COOKIE, path="/", httponly=True, secure=settings.app_env == "production", samesite="lax"
    )

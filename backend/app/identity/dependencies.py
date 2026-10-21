"""数据库会话、登录身份与写请求的来源及 CSRF 校验。"""

import hmac
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.entities import now
from app.identity.models import LoginSession, User
from app.identity.security import digest

COOKIE = "yugitalk_session"


def database(request: Request) -> Iterator[Session]:
    """为当前请求创建数据库会话，并在结束时释放资源。"""
    with Session(request.app.state.database) as db:
        yield db


def origin_guard(request: Request) -> None:
    """检查写请求来源白名单，拒绝未受信任的浏览器来源。"""
    settings: Settings = request.app.state.settings
    if request.headers.get("Origin") not in settings.auth_trusted_origins:
        raise HTTPException(403)


@dataclass
class Principal:
    """携带已验证的用户、登录会话与会话令牌。"""

    user: User
    session: LoginSession
    token: str


DB = Annotated[Session, Depends(database)]


def authenticated(request: Request, db: DB) -> Principal:
    """校验会话令牌、到期时间及用户启用状态。"""
    token = request.cookies.get(COOKIE, "")
    if not token or len(token) > 128:
        raise HTTPException(401)
    settings: Settings = request.app.state.settings
    session = db.get(
        # 浏览器持有随机令牌，数据库仅存带服务端密钥的摘要；查库时重新派生。
        LoginSession, digest(settings.session_secret.get_secret_value(), "session", token)
    )
    if session is None:
        raise HTTPException(401)
    expires = session.expires_at
    # SQLite 单测返回无时区值；PostgreSQL 的有时区值按真实偏移换算。
    expires = expires.replace(tzinfo=UTC) if expires.tzinfo is None else expires.astimezone(UTC)
    if expires <= now():
        raise HTTPException(401)
    user = db.get(User, session.user_id)
    # 每次请求都重查用户状态，停用账号后已有 Cookie 也不能继续访问。
    if user is None or not user.active:
        raise HTTPException(401)
    return Principal(user, session, token)


Actor = Annotated[Principal, Depends(authenticated)]


def csrf_actor(request: Request, actor: Actor) -> Principal:
    """验证来源和防伪令牌后返回允许写入的当前身份。"""
    origin_guard(request)
    settings: Settings = request.app.state.settings
    expected = digest(settings.session_secret.get_secret_value(), "csrf", actor.token)
    # Cookie 会被浏览器自动发送，额外的来源与 CSRF 请求头证明写入来自受信任页面。
    if not hmac.compare_digest(expected.encode(), request.headers.get("X-CSRF-Token", "").encode()):
        raise HTTPException(403)
    return actor


Writer = Annotated[Principal, Depends(csrf_actor)]

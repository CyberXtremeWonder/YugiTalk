"""邮箱验证码发放与原子注册，邮箱唯一性由数据库最终保证。"""

import hmac
import secrets
import smtplib
import ssl
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import cast

from fastapi import HTTPException, Request
from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.service import audit
from app.core.config import Settings
from app.core.entities import now
from app.identity.contracts import RegisterInput
from app.identity.models import EmailVerification, User
from app.identity.security import digest, hasher


def mail_ready(settings: Settings) -> bool:
    """公开是否具备发信配置，不公开SMTP账号或密钥。"""
    return bool(
        settings.smtp_host
        and settings.smtp_username
        and settings.smtp_password.get_secret_value()
        and settings.smtp_sender
    )


def send_code(settings: Settings, email: str, code: str) -> None:
    """使用受验证TLS发送单封验证码邮件，不输出验证码、凭证或SMTP异常正文。"""
    message = EmailMessage()
    message["From"] = settings.smtp_sender
    message["To"] = email
    message["Subject"] = "YugiTalk 注册验证码"
    message.set_content(
        f"你的注册验证码为：{code}\n10分钟内有效，请勿向他人提供。\n若不是你本人申请，请忽略本邮件。"
    )
    context = ssl.create_default_context()
    connection: smtplib.SMTP
    if settings.smtp_tls == "ssl":
        connection = smtplib.SMTP_SSL(
            settings.smtp_host, settings.smtp_port, timeout=10, context=context
        )
    else:
        connection = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10)
    with connection as client:
        if settings.smtp_tls == "starttls":
            client.starttls(context=context)
        client.login(settings.smtp_username, settings.smtp_password.get_secret_value())
        if client.send_message(message):
            raise RuntimeError("邮件接收方被拒绝")


def utc(value: datetime) -> datetime:
    """兼容SQLite测试的无时区时间与PostgreSQL的UTC时间。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def code_digest(settings: Settings, email: str, code: str) -> str:
    """将验证码绑定邮箱和独立HMAC用途，防止摘要离线枚举和跨邮箱使用。"""
    return digest(
        settings.session_secret.get_secret_value(), "registration-code", email + ":" + code
    )


def require_registration(request: Request, email: str) -> Settings:
    """验证注册开关并执行多实例共享限流，缺失发信服务时拒绝假发送。"""
    settings: Settings = request.app.state.settings
    if not settings.registration_enabled:
        raise HTTPException(403, detail={"code": "registration_disabled"})
    if not mail_ready(settings):
        raise HTTPException(503, detail={"code": "registration_mail_unavailable"})
    request.app.state.registration_limiter(
        request.client.host if request.client else "unknown", email
    )
    return settings


def issue_code(request: Request, db: Session, email: str) -> None:
    """按邮箱限制重发；SMTP失败使本次验证码无效，已有账号明确提示已注册。"""
    settings = require_registration(request, email)
    if db.scalar(select(User.id).where(func.lower(User.email) == email)):
        raise HTTPException(409, detail={"code": "email_already_registered"})
    timestamp = now()
    code = f"{secrets.randbelow(1_000_000):06d}"
    hashed = code_digest(settings, email, code)
    values = {
        "code_hash": hashed,
        "expires_at": timestamp + timedelta(minutes=10),
        "sent_at": timestamp,
        "attempts": 0,
        "consumed": False,
        "delivered": False,
    }
    old = db.get(EmailVerification, email)
    if old:
        changed = db.execute(
            update(EmailVerification)
            .where(
                EmailVerification.email == email,
                EmailVerification.sent_at <= timestamp - timedelta(seconds=60),
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if cast(CursorResult[object], changed).rowcount != 1:
            db.rollback()
            raise HTTPException(429, headers={"Retry-After": "60"})
    else:
        db.add(EmailVerification(email=email, **values))
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(429, headers={"Retry-After": "60"}) from error
    try:
        request.app.state.send_verification_code(settings, email, code)
    except Exception as error:
        db.execute(
            delete(EmailVerification).where(
                EmailVerification.email == email, EmailVerification.code_hash == hashed
            )
        )
        db.commit()
        raise HTTPException(503, detail={"code": "registration_mail_unavailable"}) from error
    db.execute(
        update(EmailVerification)
        .where(EmailVerification.email == email, EmailVerification.code_hash == hashed)
        .values(delivered=True)
    )
    db.commit()


def register_user(request: Request, db: Session, data: RegisterInput) -> None:
    """验证码领取与用户创建在同一事务提交，禁止重放和并发重复注册。"""
    settings = require_registration(request, data.email)
    if db.scalar(select(User.id).where(func.lower(User.email) == data.email)):
        raise HTTPException(409, detail={"code": "email_already_registered"})
    challenge = db.get(EmailVerification, data.email)
    valid = bool(
        challenge
        and challenge.delivered
        and not challenge.consumed
        and challenge.attempts < 5
        and utc(challenge.expires_at) > now()
        and hmac.compare_digest(challenge.code_hash, code_digest(settings, data.email, data.code))
    )
    if not valid:
        db.execute(
            update(EmailVerification)
            .where(
                EmailVerification.email == data.email,
                EmailVerification.consumed.is_(False),
                EmailVerification.attempts < 5,
            )
            .values(attempts=EmailVerification.attempts + 1)
        )
        db.commit()
        raise HTTPException(422, detail={"code": "verification_code_invalid"})
    assert challenge is not None
    changed = db.execute(
        update(EmailVerification)
        .where(
            EmailVerification.email == data.email,
            EmailVerification.code_hash == challenge.code_hash,
            EmailVerification.consumed.is_(False),
            EmailVerification.delivered.is_(True),
            EmailVerification.attempts < 5,
            EmailVerification.expires_at > now(),
        )
        .values(consumed=True)
        .execution_options(synchronize_session=False)
    )
    if cast(CursorResult[object], changed).rowcount != 1:
        db.rollback()
        raise HTTPException(422, detail={"code": "verification_code_invalid"})
    user = User(
        email=data.email,
        name=data.name,
        password_hash=hasher.hash(data.password),
        platform_admin=False,
    )
    db.add(user)
    try:
        db.flush()
        audit(db, user, "auth.register")
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(409, detail={"code": "email_already_registered"}) from error

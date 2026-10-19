"""密码使用 Argon2id，会话与 CSRF 分域派生，数据库不保存明文令牌。"""

import hashlib
import hmac
import secrets
from threading import BoundedSemaphore

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

hasher = PasswordHasher()
dummy_hash = hasher.hash(secrets.token_urlsafe(32))
# Argon2 每次校验约需 64 MiB；限制单进程并发以控制内存峰值。
verification_slots = BoundedSemaphore(2)


def verify_password(stored: str, password: str) -> bool:
    """校验密码摘要，未知账号也执行相同的摘要验证路径。"""
    try:
        with verification_slots:
            return hasher.verify(stored, password)
    except (VerificationError, InvalidHashError):
        return False


def digest(secret: str, purpose: str, token: str) -> str:
    """按用途计算带密钥的摘要，隔离会话与防伪令牌。"""
    return hmac.new(secret.encode(), f"{purpose}:{token}".encode(), hashlib.sha256).hexdigest()

"""身份与用户资源关系模型；类型、唯一约束和外键共同保护一致性。"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.orm import DeclarativeBase


def now() -> datetime:
    """返回带时区的当前时间，统一租约、过期与审计时间基准。"""
    return datetime.now(UTC)


def identifier() -> str:
    """生成随机唯一标识，作为新实体的默认主键。"""
    return str(uuid4())


class Base(DeclarativeBase):
    """提供各领域实体共享的声明式映射基类与数据库元数据。"""

    pass

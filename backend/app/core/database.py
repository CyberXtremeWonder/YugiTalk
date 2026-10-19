"""数据库引擎只在首次使用时连接，请求结束自动释放会话。"""

from sqlalchemy import Engine, create_engine

# API、Worker、Scheduler 共用此入口，独立进程也须加载完整实体才能解析外键。
from app.core import model_registry  # noqa: F401
from app.core.config import Settings


def create_database(settings: Settings) -> Engine:
    """从受控配置建立数据库连接池，统一使用实际驱动。"""
    url = (
        settings.database_url.get_secret_value()
        .replace("postgresql://", "postgresql+psycopg://", 1)
        .replace("postgres://", "postgresql+psycopg://", 1)
    )
    return create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})

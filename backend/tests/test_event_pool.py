"""验证SSE握手在连接池已占满时仍释放请求连接，避免建立事件流死锁。"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.core.config import Settings
from app.tasks import router
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool


def test_event_handshake_releases_connection_before_revalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """仅有一条连接时，初次鉴权事务须先归还连接，复核事件权限才可继续。"""
    engine = create_engine(
        "sqlite://", poolclass=QueuePool, pool_size=1, max_overflow=0, pool_timeout=0.01
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(database=engine, settings=Settings(app_env="test"))
        )
    )
    monkeypatch.setattr(router, "owned_run", lambda *_: SimpleNamespace(event_sequence=0))

    def revalidate(*_: object) -> tuple[list[dict[str, object]], bool]:
        """以真实连接池复核事务；旧实现会因请求事务占着唯一连接而超时。"""
        with engine.connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar() == 1
        return [], False

    monkeypatch.setattr(router, "poll_events", revalidate)
    try:
        with Session(engine) as db:
            db.execute(text("SELECT 1"))
            response = router.events(uuid4(), request, db, None)
            assert response.media_type == "text/event-stream"
            assert not db.in_transaction()
    finally:
        engine.dispose()

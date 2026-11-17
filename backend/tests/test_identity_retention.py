"""身份过期清理的只读预览、批次、回滚与并发刷新保护回归。"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, event, func, select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.entities import Base
from app.identity.models import EmailVerification, LoginSession, User
from app.identity.retention import cleanup_expired, inspect_expired

SAMPLED = datetime(2026, 11, 3, 8, tzinfo=UTC)


@pytest.fixture
def engine() -> Iterator[Engine]:
    """只建立内存数据库及身份表，测试结束释放所有连接。"""
    database = create_engine("sqlite://")
    Base.metadata.create_all(
        database,
        tables=[User.__table__, LoginSession.__table__, EmailVerification.__table__],
    )
    with Session(database) as db:
        db.add(User(id="owner", email="owner@example.test", name="测试", password_hash="test"))
        db.commit()
    yield database
    database.dispose()


def seed(database: Engine, offsets: list[int]) -> None:
    """为两表建立相同过期边界，同时包含已消费但仍有效的验证码。"""
    with Session(database) as db:
        for number, offset in enumerate(offsets):
            expiry = SAMPLED + timedelta(seconds=offset)
            db.add(LoginSession(token_hash=f"token-{number}", user_id="owner", expires_at=expiry))
            db.add(
                EmailVerification(
                    email=f"code-{number}@example.test",
                    code_hash=f"hash-{number}",
                    expires_at=expiry,
                    sent_at=SAMPLED - timedelta(minutes=10),
                    delivered=True,
                    consumed=offset > 0,
                    attempts=0,
                )
            )
        db.commit()


def counts(database: Engine) -> tuple[int, int]:
    """读取两张身份表的行数，不读取邮箱或令牌内容。"""
    with Session(database) as db:
        return (
            db.scalar(select(func.count()).select_from(LoginSession)) or 0,
            db.scalar(select(func.count()).select_from(EmailVerification)) or 0,
        )


def test_preview_has_no_writes_or_autoflush(engine: Engine) -> None:
    """预览返回聚合数据，截止点等于失效时间的行可清理，不触发会话自动写入。"""
    seed(engine, [-60, 0, 60])
    statements = []

    def capture(connection: Any, cursor: Any, statement: str, *arguments: Any) -> None:
        """只记录SQL种类，验证预览不产生INSERT、UPDATE或DELETE。"""
        statements.append(statement.lstrip().split()[0].upper())

    event.listen(engine, "before_cursor_execute", capture)
    with Session(engine) as db:
        db.add(
            User(id="pending", email="pending@example.test", name="未保存", password_hash="test")
        )
        report = inspect_expired(db, SAMPLED)
        assert report == {
            name: {
                "rows": 3,
                "expired": 2,
                "oldest_expires_at": (SAMPLED - timedelta(seconds=60)).isoformat(),
            }
            for name in ("login_sessions", "email_verifications")
        }
        assert len(db.new) == 1
    assert statements == ["SELECT", "SELECT"]
    event.remove(engine, "before_cursor_execute", capture)
    assert counts(engine) == (3, 3)


def test_cleanup_is_bounded_preserves_valid_rows_and_is_repeatable(engine: Engine) -> None:
    """每表只删限定批次；有效会话和已消费但未过期的验证码保持完整。"""
    seed(engine, [-90, -60, -30, 0, 60])
    first = cleanup_expired(engine, cutoff=SAMPLED, batch_size=2)
    assert first == {"cutoff": SAMPLED.isoformat(), "login_sessions": 2, "email_verifications": 2}
    assert counts(engine) == (3, 3)
    second = cleanup_expired(engine, cutoff=SAMPLED, batch_size=2)
    assert second["login_sessions"] == second["email_verifications"] == 2
    third = cleanup_expired(engine, cutoff=SAMPLED, batch_size=2)
    assert third["login_sessions"] == third["email_verifications"] == 0
    assert counts(engine) == (1, 1)
    with Session(engine) as db:
        code = db.get(EmailVerification, "code-4@example.test")
        assert code is not None and code.consumed
        assert db.get(LoginSession, "token-4") is not None
        assert db.get(User, "owner") is not None


def test_default_batch_size_is_five_hundred(engine: Engine) -> None:
    """未传批次时每表最多清理五百条，不一次清空全部历史。"""
    seed(engine, [-60] * 501)
    result = cleanup_expired(engine, cutoff=SAMPLED)
    assert result["login_sessions"] == result["email_verifications"] == 500
    assert counts(engine) == (1, 1)


def test_default_cutoff_is_sampled_once(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """一次清理只采样一次时间，两表使用同一截止点保护稍后才过期的记录。"""
    seed(engine, [-60, 10])
    samples = []

    def sample() -> datetime:
        """如果错误地重复采样，返回后续时间以暴露清理边界漂移。"""
        value = SAMPLED + timedelta(seconds=20 * len(samples))
        samples.append(value)
        return value

    monkeypatch.setattr("app.identity.retention.now", sample)
    result = cleanup_expired(engine)
    assert samples == [SAMPLED]
    assert result["cutoff"] == SAMPLED.isoformat()
    assert counts(engine) == (1, 1)


@pytest.mark.parametrize("size", [0, -1, 1001])
def test_invalid_batch_size_cannot_delete(engine: Engine, size: int) -> None:
    """非法或超过上限的批次在产生数据库删除之前失败。"""
    seed(engine, [-60])
    with pytest.raises(ValueError, match="batch_size"):
        cleanup_expired(engine, cutoff=SAMPLED, batch_size=size)
    assert counts(engine) == (1, 1)


def test_empty_preview_and_utc_cutoff(engine: Engine) -> None:
    """空表无最早时间，带偏移时间转换UTC，拒绝无时区截止点。"""
    offset = SAMPLED.astimezone(timezone(timedelta(hours=8)))
    result = cleanup_expired(engine, cutoff=offset)
    assert result == {"cutoff": SAMPLED.isoformat(), "login_sessions": 0, "email_verifications": 0}
    with Session(engine) as db:
        assert inspect_expired(db, SAMPLED) == {
            name: {"rows": 0, "expired": 0, "oldest_expires_at": None}
            for name in ("login_sessions", "email_verifications")
        }
        with pytest.raises(ValueError, match="UTC|timezone|时区"):
            inspect_expired(db, SAMPLED.replace(tzinfo=None))
    with pytest.raises(ValueError, match="UTC|timezone|时区"):
        cleanup_expired(engine, cutoff=SAMPLED.replace(tzinfo=None))


def test_failure_rolls_back_both_tables(engine: Engine) -> None:
    """验证码删除失败时，前面已经执行的会话删除也必须回滚。"""
    seed(engine, [-60])
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER refuse_cleanup BEFORE DELETE ON email_verifications "
                "BEGIN SELECT RAISE(ABORT, 'test cleanup failure'); END"
            )
        )
    with pytest.raises(SQLAlchemyError):
        cleanup_expired(engine, cutoff=SAMPLED)
    assert counts(engine) == (1, 1)


def test_refreshed_verification_is_rechecked_before_delete(engine: Engine) -> None:
    """模拟候选邮箱选中后续发新码，DELETE仍检查失效时间以保护刷新记录。"""
    seed(engine, [-60])
    refreshed = False

    def refresh(connection: Any, cursor: Any, statement: str, *arguments: Any) -> None:
        """在候选查询完成后更新验证码，模拟删除前记录版本已变化。"""
        nonlocal refreshed
        if statement.startswith("SELECT email_verifications.email") and not refreshed:
            refreshed = True
            connection.execute(
                update(EmailVerification)
                .where(EmailVerification.email == "code-0@example.test")
                .values(expires_at=SAMPLED + timedelta(minutes=10), code_hash="refreshed")
            )

    event.listen(engine, "after_cursor_execute", refresh)
    result = cleanup_expired(engine, cutoff=SAMPLED)
    event.remove(engine, "after_cursor_execute", refresh)
    assert refreshed and result["email_verifications"] == 0
    assert counts(engine) == (0, 1)
    with Session(engine) as db:
        code = db.get(EmailVerification, "code-0@example.test")
        assert code is not None and code.code_hash == "refreshed"


def test_postgresql_sql_locks_candidates_and_rechecks_expiry(engine: Engine) -> None:
    """SQLite执行的同一语句用PostgreSQL编译时必须带跳锁、批次和删除条件。"""
    seed(engine, [-60])
    statements = []

    def capture(connection: Any, statement: Any, *arguments: Any) -> None:
        """保留实际执行的SQLAlchemy语句，以验证生产方言生成的SQL边界。"""
        statements.append(str(statement.compile(dialect=postgresql.dialect())))

    event.listen(engine, "before_execute", capture)
    cleanup_expired(engine, cutoff=SAMPLED, batch_size=1)
    event.remove(engine, "before_execute", capture)
    candidates = [statement for statement in statements if statement.startswith("SELECT")]
    deletions = [statement for statement in statements if statement.startswith("DELETE")]
    assert len(candidates) == len(deletions) == 2
    assert all(
        "FOR UPDATE SKIP LOCKED" in statement and "LIMIT" in statement for statement in candidates
    )
    assert all("expires_at <=" in statement and " IN " in statement for statement in deletions)

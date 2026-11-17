"""验证身份维护开关、低频批次及失败日志脱敏，不运行恢复线程。"""

from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings
from app.tasks import scheduler


@pytest.mark.parametrize("enabled,next_due,expected", [(False, 0.0, 0.0), (True, 200.0, 200.0)])
def test_cleanup_disabled_or_not_due(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, next_due: float, expected: float
) -> None:
    """关闭或尚未到期时不连接身份表，也不运行全库盘点。"""
    engine = create_engine("sqlite://")
    settings = Settings(_env_file=None, identity_cleanup_enabled=enabled)
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: 100.0)

    def forbidden(*arguments: Any, **options: Any) -> dict[str, object]:
        """任何清理调用都代表开关或频率控制失效。"""
        raise AssertionError("不应清理")

    monkeypatch.setattr(scheduler, "cleanup_expired", forbidden)
    assert scheduler.cleanup_identity_if_due(engine, settings, next_due) == expected
    engine.dispose()


@pytest.mark.parametrize("fails", [False, True])
def test_due_cleanup_is_bounded_and_failure_isolated(
    monkeypatch: pytest.MonkeyPatch, caplog: Any, fails: bool
) -> None:
    """到期只跑一批，数据库失败也返回下一次时间且不打印SQL或凭证。"""
    engine = create_engine("sqlite://")
    settings = Settings(
        _env_file=None,
        identity_cleanup_enabled=True,
        identity_cleanup_interval_seconds=600,
        identity_cleanup_batch_size=3,
    )
    calls = []
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: 100.0)

    def cleanup(*arguments: Any, **options: Any) -> dict[str, object]:
        """模拟受控结果或携带敏感输入的数据库失败。"""
        calls.append(options)
        if fails:
            raise SQLAlchemyError("private-email@example.org private-token-hash")
        return {
            "login_sessions": 3,
            "email_verifications": 2,
            "cutoff": "2026-11-03T08:00:00+00:00",
        }

    monkeypatch.setattr(scheduler, "cleanup_expired", cleanup)
    next_due = scheduler.cleanup_identity_if_due(engine, settings, 0.0)
    assert next_due == 700.0
    assert calls == [{"batch_size": 3}]
    assert scheduler.cleanup_identity_if_due(engine, settings, next_due) == next_due
    assert len(calls) == 1
    assert "private-email" not in caplog.text and "private-token" not in caplog.text
    if fails:
        assert any(record.operation == "identity.cleanup" for record in caplog.records)
    engine.dispose()


@pytest.mark.parametrize(
    "overrides",
    [{"identity_cleanup_interval_seconds": 0}, {"identity_cleanup_batch_size": 1001}],
)
def test_cleanup_configuration_has_limits(overrides: dict[str, int]) -> None:
    """运行配置不能关闭频率边界或扩大单批上限。"""
    with pytest.raises(ValueError):
        Settings(_env_file=None, **overrides)

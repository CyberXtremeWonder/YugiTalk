"""验证维护入口默认只读、聚合脱敏与显式清理边界。"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import maintenance
from app.chat.models import Conversation, Message
from app.core.config import Settings
from app.core.entities import Base
from app.identity.models import EmailVerification, LoginSession, User
from app.research.models import ResearchProject, ResearchReport

SAMPLED = datetime(2026, 11, 3, 8, tzinfo=UTC)


@pytest.fixture
def database() -> Any:
    """创建完整隔离数据库，放入不能出现在盘点报告中的私有字段。"""
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(
            User(
                id="private-user",
                email="private@example.org",
                name="私有用户",
                password_hash="private-hash",
            )
        )
        db.flush()
        conversation = Conversation(
            id="private-conversation",
            account_id="private-user",
            owner_id="private-user",
            title="私有对话",
            created_at=SAMPLED,
        )
        db.add(conversation)
        db.flush()
        db.add(
            Message(conversation_id=conversation.id, sequence=1, role="user", content="私有正文")
        )
        project = ResearchProject(owner_id="private-user", title="私有报告", question="私有问题")
        db.add(project)
        db.flush()
        db.add(
            ResearchReport(
                project_id=project.id,
                run_id="private-run",
                version=1,
                markdown="私有报告正文",
                data={"files": {"md": {"content": "private-file-bytes"}}},
                created_at=SAMPLED,
            )
        )
        for key, expiration in (("expired", SAMPLED), ("valid", SAMPLED + timedelta(hours=1))):
            db.add(LoginSession(token_hash=key, user_id="private-user", expires_at=expiration))
            db.add(
                EmailVerification(
                    email=f"{key}@example.org",
                    code_hash="private-code",
                    expires_at=expiration,
                    sent_at=SAMPLED - timedelta(minutes=10),
                    attempts=0,
                    consumed=False,
                    delivered=True,
                )
            )
        db.commit()
    yield engine
    engine.dispose()


def test_inventory_is_read_only_and_contains_only_aggregates(database: Engine) -> None:
    """报告只执行SELECT，不提交或加载业务正文，并明确外部存储未实测。"""
    statements: list[str] = []
    commits: list[bool] = []

    def record(connection: Any, cursor: Any, statement: str, *arguments: Any) -> None:
        """记录实际SQL种类，防止只读盘点夹带数据库写入。"""
        statements.append(statement)

    event.listen(database, "before_cursor_execute", record)
    event.listen(database, "commit", lambda connection: commits.append(True))
    report = maintenance.inventory(database, cutoff=SAMPLED, batch_size=1)
    assert statements and all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
    assert not commits
    assert report["tables"]["research_reports"]["rows"] == 1
    assert report["tables"]["research_reports"]["allocated_bytes"] is None
    assert report["auth_cleanup_preview"]["login_sessions"]["expired"] == 1
    assert report["auth_cleanup_preview"]["email_verifications"]["next_batch"] == 1
    assert report["external_storage"]["measured"] is False
    output = json.dumps(report, ensure_ascii=False)
    for private in (
        "private@example.org",
        "private-hash",
        "私有正文",
        "private-file-bytes",
        "private-code",
    ):
        assert private not in output


@pytest.mark.parametrize(
    "arguments,applied", [([], False), (["--dry-run"], False), (["--apply-auth-cleanup"], True)]
)
def test_cli_requires_explicit_apply(
    database: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
    arguments: list[str],
    applied: bool,
) -> None:
    """普通执行与dry-run均不调用清理，只有显式参数允许有界删除。"""
    calls = []

    def cleanup(engine: Engine, cutoff: datetime, batch_size: int) -> dict[str, object]:
        """记录清理授权与批量上限，CLI测试不执行数据库删除。"""
        calls.append((cutoff, batch_size))
        return {"cutoff": cutoff.isoformat(), "login_sessions": 0, "email_verifications": 0}

    monkeypatch.setattr(maintenance, "load_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(maintenance, "create_database", lambda settings: database)
    monkeypatch.setattr(maintenance, "cleanup_expired", cleanup)
    monkeypatch.setattr(maintenance, "now", lambda: SAMPLED)
    monkeypatch.setattr("sys.argv", ["maintenance", "--batch-size", "1", *arguments])
    assert maintenance.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == ("apply_auth_cleanup" if applied else "dry_run")
    assert bool(calls) is applied
    if applied:
        assert calls == [(SAMPLED, 1)]


@pytest.mark.parametrize("size", ["0", "1001"])
def test_invalid_batch_refused_before_connecting(
    size: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无效批量上限在建立连接前拒绝，不能扩大清理范围。"""
    monkeypatch.setattr("sys.argv", ["maintenance", "--batch-size", size])

    def forbidden(settings: Settings) -> Engine:
        """拒绝任何数据库连接，确保参数校验发生在连接之前。"""
        raise AssertionError("不应连接数据库")

    monkeypatch.setattr(maintenance, "create_database", forbidden)
    with pytest.raises(SystemExit) as result:
        maintenance.main()
    assert result.value.code == 2


def test_cli_does_not_print_private_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """配置异常只显示类别，避免连接地址或凭证进入维护日志。"""
    monkeypatch.setattr("sys.argv", ["maintenance"])

    def fail() -> Settings:
        """模拟含有私密连接地址的校验错误。"""
        raise ValueError("private-database-password")

    monkeypatch.setattr(maintenance, "load_settings", fail)
    assert maintenance.main() == 1
    assert "private-database-password" not in str(capsys.readouterr())

"""邮箱注册、管理员开发模式和工具审核的安全与功能回归，不发送真实邮件。"""

from datetime import timedelta
from typing import Any
from unittest.mock import Mock

import pytest
from app.core.entities import now
from app.identity.models import EmailVerification, LoginSession, User
from app.identity.registration import code_digest, send_code
from app.identity.security import hasher
from app.providers.client import ProviderFailure
from app.tools.models import ManagedTool, ToolSubmission
from app.tools.service import bound_tool, freeze_tools, validate_names
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_identity import ORIGIN, PASSWORD, login
from test_tools_management import tool_data

pytest_plugins = ["test_identity"]


def configure_mail(client: TestClient) -> dict[str, str]:
    """配置虚构SMTP并将邮件替换为内存收件箱，验证码不出测试进程。"""
    settings = client.app.state.settings
    settings.smtp_host = "smtp.example.test"
    settings.smtp_username = "sender@example.test"
    settings.smtp_password = SecretStr("test-mail-password")
    settings.smtp_sender = "sender@example.test"
    outbox: dict[str, str] = {}

    def deliver(settings: Any, email: str, code: str) -> None:
        """只在内存记录测试验证码，绝不访问SMTP。"""
        outbox[email] = code

    client.app.state.send_verification_code = deliver
    return outbox


def register_data(email: str, code: str) -> dict[str, str]:
    """构造普通注册请求，密码长度沿用用户要求的不设限制行为。"""
    return {"email": email, "name": "新用户", "password": "x", "code": code}


def admin_login(client: TestClient, mode: bool = True) -> dict[str, Any]:
    """以已有平台管理员登录，模式由服务端会话保存。"""
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.test", "password": PASSWORD, "development_mode": mode},
        headers=ORIGIN,
    )
    assert response.status_code == 200
    return response.json()


def test_signup_verified_unique_and_no_privilege(client: TestClient) -> None:
    """邮箱验证后只能创建一个普通账号，大小写邮箱、重复注册和验证码重放被拒绝。"""
    outbox = configure_mail(client)
    result = client.post(
        "/api/v1/auth/registration-code", json={"email": "New@Example.test"}, headers=ORIGIN
    )
    assert result.status_code == 200
    code = outbox["new@example.test"]
    assert code not in result.text
    with Session(client.app.state.database) as db:
        challenge = db.get(EmailVerification, "new@example.test")
        assert challenge and challenge.code_hash != code
    response = client.post(
        "/api/v1/auth/register", json=register_data(" NEW@example.test ", code), headers=ORIGIN
    )
    assert response.status_code == 201
    assert (
        client.post(
            "/api/v1/auth/register", json=register_data("new@example.test", code), headers=ORIGIN
        ).status_code
        == 409
    )
    identity = client.post(
        "/api/v1/auth/login", json={"email": "new@example.test", "password": "x"}, headers=ORIGIN
    ).json()
    assert identity["user"]["platform_admin"] is False
    assert "workspaces" not in identity
    assert (
        client.post(
            "/api/v1/auth/register",
            json={**register_data("other-new@example.test", code), "platform_admin": True},
            headers=ORIGIN,
        ).status_code
        == 422
    )


@pytest.mark.parametrize("kind", ["expired", "undelivered", "consumed", "missing", "wrong_email"])
def test_invalid_verification_state(client: TestClient, kind: str) -> None:
    """过期、未发送、已消费、缺失和跨邮箱验证码不能创建账号。"""
    configure_mail(client)
    email = "new@example.test"
    if kind != "missing":
        with Session(client.app.state.database) as db:
            db.add(
                EmailVerification(
                    email=email if kind != "wrong_email" else "other-new@example.test",
                    code_hash=code_digest(client.app.state.settings, email, "123456"),
                    sent_at=now(),
                    expires_at=now() + timedelta(seconds=-1 if kind == "expired" else 600),
                    delivered=kind != "undelivered",
                    consumed=kind == "consumed",
                )
            )
            db.commit()
    assert (
        client.post(
            "/api/v1/auth/register", json=register_data(email, "123456"), headers=ORIGIN
        ).status_code
        == 422
    )
    with Session(client.app.state.database) as db:
        assert db.scalar(select(User).where(User.email == email)) is None


def test_wrong_code_attempts_and_resend(client: TestClient) -> None:
    """五次猜错后正确验证码也失效，60秒重发限制由服务端执行。"""
    outbox = configure_mail(client)
    client.post(
        "/api/v1/auth/registration-code", json={"email": "new@example.test"}, headers=ORIGIN
    )
    code = outbox["new@example.test"]
    assert (
        client.post(
            "/api/v1/auth/registration-code", json={"email": "new@example.test"}, headers=ORIGIN
        ).status_code
        == 429
    )
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        assert (
            client.post(
                "/api/v1/auth/register",
                json=register_data("new@example.test", wrong),
                headers=ORIGIN,
            ).status_code
            == 422
        )
    assert (
        client.post(
            "/api/v1/auth/register", json=register_data("new@example.test", code), headers=ORIGIN
        ).status_code
        == 422
    )


def test_mail_failure_and_missing_service(client: TestClient) -> None:
    """未配置或SMTP失败不返回假成功，也不保留可用于注册的验证码。"""
    client.app.state.settings.smtp_host = ""
    assert (
        client.post(
            "/api/v1/auth/registration-code", json={"email": "new@example.test"}, headers=ORIGIN
        ).status_code
        == 503
    )
    configure_mail(client)
    client.app.state.send_verification_code = Mock(
        side_effect=RuntimeError("do-not-expose-mail-secret")
    )
    response = client.post(
        "/api/v1/auth/registration-code", json={"email": "new@example.test"}, headers=ORIGIN
    )
    assert response.status_code == 503
    assert "do-not-expose-mail-secret" not in response.text
    with Session(client.app.state.database) as db:
        assert db.get(EmailVerification, "new@example.test") is None


def test_registered_email_code_request_is_explicit(client: TestClient) -> None:
    """获取验证码时也明确提示邮箱已注册，不发送邮件或产生验证码记录。"""
    outbox = configure_mail(client)
    response = client.post(
        "/api/v1/auth/registration-code",
        json={"email": "OWNER@example.test"},
        headers=ORIGIN,
    )
    assert response.status_code == 409
    assert response.json()["code"] == "email_already_registered"
    assert "该邮箱已注册" in response.json()["message"]
    assert outbox == {}


def test_email_constraint_works_without_api(client: TestClient) -> None:
    """数据库规范邮箱唯一索引阻止绕过接口或并发插入大小写重复账号。"""
    with Session(client.app.state.database) as db:
        db.add(User(email="OWNER@example.test", name="duplicate", password_hash=hasher.hash("x")))
        with pytest.raises(IntegrityError):
            db.commit()


def test_development_mode_authorization_and_revocation(client: TestClient) -> None:
    """普通用户不能勾选模式提权，管理员模式持久保存且角色撤销立即生效。"""
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "member@example.test", "password": PASSWORD, "development_mode": True},
        headers=ORIGIN,
    )
    assert response.status_code == 403
    assert client.cookies.get("yugitalk_session") is None
    admin_login(client, False)
    assert client.get("/api/v1/admin/users").status_code == 403
    identity = admin_login(client)
    assert identity["development_mode"] is True
    assert client.get("/api/v1/auth/me").json()["development_mode"] is True
    assert client.get("/api/v1/admin/users").status_code == 200
    with Session(client.app.state.database) as db:
        user = db.get(User, identity["user"]["id"])
        assert user
        user.platform_admin = False
        db.commit()
    assert client.get("/api/v1/admin/users").status_code == 403


def test_admin_username_and_account_disable(client: TestClient) -> None:
    """管理员用户名支持大小写规范，停用普通账号撤销会话，管理账号受保护。"""
    with Session(client.app.state.database) as db:
        admin = db.scalar(select(User).where(User.email == "owner@example.test"))
        assert admin
        admin.username = "cxwadmin"
        member = db.scalar(select(User).where(User.email == "member@example.test"))
        assert member
        member_id = member.id
        db.commit()
    login(client, "member@example.test")
    result = client.post(
        "/api/v1/auth/login",
        json={"email": "cxwAdmin", "password": PASSWORD, "development_mode": True},
        headers=ORIGIN,
    )
    assert result.status_code == 200
    identity = result.json()
    headers = {**ORIGIN, "X-CSRF-Token": identity["csrf_token"]}
    assert (
        client.patch(
            f"/api/v1/admin/users/{member_id}", json={"active": False}, headers=headers
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"/api/v1/admin/users/{identity['user']['id']}", json={"active": False}, headers=headers
        ).status_code
        == 409
    )
    with Session(client.app.state.database) as db:
        assert not db.scalars(select(LoginSession).where(LoginSession.user_id == member_id)).all()


def test_tool_submission_approval_and_duplicate_review(client: TestClient) -> None:
    """成员可提交但不能发布或审核，批准后才进入目录，重复审核被拒绝。"""
    identity = login(client, "member@example.test")
    space = identity["user"]["id"]
    headers = {**ORIGIN, "X-CSRF-Token": identity["csrf_token"]}
    response = client.post(
        f"/api/v1/me/tool-submissions", json=tool_data(), headers=headers
    )
    assert response.status_code == 201
    submission = response.json()
    with Session(client.app.state.database) as db:
        assert not db.scalars(select(ManagedTool)).all()
    assert client.get("/api/v1/admin/tool-submissions").status_code == 403
    admin = admin_login(client)
    admin_headers = {**ORIGIN, "X-CSRF-Token": admin["csrf_token"]}
    endpoint = f"/api/v1/admin/tool-submissions/{submission['id']}/review"
    assert (
        client.post(endpoint, json={"decision": "approved"}, headers=admin_headers).status_code
        == 200
    )
    assert (
        client.post(endpoint, json={"decision": "approved"}, headers=admin_headers).status_code
        == 409
    )
    with Session(client.app.state.database) as db:
        tool = db.scalar(select(ManagedTool).where(ManagedTool.account_id == space))
        assert tool and tool.owner_id == identity["user"]["id"]
    # 即使同空间管理员也不能在普通工具目录看到或引用他人的个人工具。
    assert "public_lookup" not in [
        item["name"] for item in client.get(f"/api/v1/me/tools").json()["items"]
    ]
    settings = client.app.state.settings
    settings.tool_http_allowed_hosts = ["api.example.com"]
    with Session(client.app.state.database) as db:
        pins = freeze_tools(db, space, identity["user"]["id"], ["public_lookup"], settings)
        assert "public_lookup" in pins
        with pytest.raises(HTTPException):
            freeze_tools(db, space, admin["user"]["id"], ["public_lookup"], settings)
        with pytest.raises(HTTPException):
            validate_names(db, space, ["public_lookup"], admin["user"]["id"])
        with pytest.raises(ProviderFailure, match="permission_revoked"):
            bound_tool(
                db,
                space,
                admin["user"]["id"],
                {"tools": ["public_lookup"], "tool_catalog": pins},
                "public_lookup",
                settings,
            )
    assert (
        client.patch(
            f"/api/v1/me/tools/public_lookup",
            json={"enabled": False},
            headers=admin_headers,
        ).status_code
        == 404
    )


def test_workspace_owner_cannot_bypass_review(client: TestClient) -> None:
    """普通用户即使拥有个人空间，也不能直接创建或发布工具来绕过平台审核。"""
    identity = login(client, "other@example.test")
    space = identity["user"]["id"]
    headers = {**ORIGIN, "X-CSRF-Token": identity["csrf_token"]}
    response = client.post(f"/api/v1/me/tools", json=tool_data(), headers=headers)
    assert response.status_code == 403
    assert response.json()["code"] == "tool_review_required"


@pytest.mark.parametrize("reason", ["rejected", "tampered", "revoked"])
def test_tool_review_failure_boundaries(client: TestClient, reason: str) -> None:
    """驳回不发布工具，篡改或提交者权限撤销阻止批准。"""
    user = login(client, "member@example.test")
    space = user["user"]["id"]
    response = client.post(
        f"/api/v1/me/tool-submissions",
        json=tool_data(),
        headers={**ORIGIN, "X-CSRF-Token": user["csrf_token"]},
    )
    submission_id = response.json()["id"]
    if reason != "rejected":
        with Session(client.app.state.database) as db:
            if reason == "tampered":
                submission = db.get(ToolSubmission, submission_id)
                assert submission
                submission.config = {**submission.config, "endpoint": "https://evil.example.com"}
            else:
                member = db.get(User, user["user"]["id"])
                assert member
                member.active = False
            db.commit()
    admin = admin_login(client)
    endpoint = f"/api/v1/admin/tool-submissions/{submission_id}/review"
    headers = {**ORIGIN, "X-CSRF-Token": admin["csrf_token"]}
    if reason == "rejected":
        assert (
            client.post(endpoint, json={"decision": "rejected"}, headers=headers).status_code == 422
        )
        assert (
            client.post(
                endpoint, json={"decision": "rejected", "reason": "用途需要补充"}, headers=headers
            ).status_code
            == 200
        )
    else:
        assert (
            client.post(endpoint, json={"decision": "approved"}, headers=headers).status_code == 409
        )
    with Session(client.app.state.database) as db:
        assert not db.scalars(select(ManagedTool)).all()


@pytest.mark.parametrize("tls", ["ssl", "starttls"])
def test_smtp_uses_verified_tls(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tls: str
) -> None:
    """两种SMTP连接均验证证书，凭证与验证码只进入邮件发送层。"""
    import ssl

    from app.identity import registration

    configure_mail(client)
    settings = client.app.state.settings
    settings.smtp_tls = tls
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.send_message.return_value = {}
    factory = Mock(return_value=connection)
    monkeypatch.setattr(registration.smtplib, "SMTP_SSL" if tls == "ssl" else "SMTP", factory)
    send_code(settings, "new@example.test", "123456")
    context = (
        factory.call_args.kwargs["context"]
        if tls == "ssl"
        else connection.starttls.call_args.kwargs["context"]
    )
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    message = connection.send_message.call_args.args[0]
    assert "123456" in message.get_content()

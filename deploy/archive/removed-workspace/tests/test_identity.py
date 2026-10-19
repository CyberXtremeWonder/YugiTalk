"""身份与 RBAC 的边界回归，SQLite 用于隔离单测，真实 PostgreSQL 另行集成验收。"""

from collections.abc import Iterator
from datetime import timedelta
from io import StringIO

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.audit.models import AuditEvent
from app.core.config import Settings
from app.core.entities import Base, now
from app.identity import bootstrap
from app.identity.models import LoginSession, User
from app.identity.security import hasher
from app.main import create_app
from app.workspace.models import Membership, Workspace

PASSWORD = "Test-only-password-24"
ORIGIN = {"Origin": "http://testserver"}


def test_admin_can_enable_panel_after_normal_login(client: TestClient) -> None:
    """管理员普通登录后可显式开启面板，普通用户和无防伪请求不能开启。"""
    identity = login(client)
    assert identity["development_mode"] is False
    assert client.post("/api/v1/auth/development-mode", headers=ORIGIN).status_code == 403
    response = client.post(
        "/api/v1/auth/development-mode",
        headers={**ORIGIN, "X-CSRF-Token": identity["csrf_token"]},
    )
    assert response.status_code == 200
    assert client.get("/api/v1/auth/me").json()["development_mode"] is True
    member = login(client, "member@example.test")
    assert client.post(
        "/api/v1/auth/development-mode",
        headers={**ORIGIN, "X-CSRF-Token": member["csrf_token"]},
    ).status_code == 403


def test_workspace_delete_owner_hides_and_revokes_access(client: TestClient) -> None:
    """所有者删除空间后隐藏列表并拒绝原空间读写，其他空间不受影响。"""
    identity = login(client)
    space = identity["workspaces"][0]["id"]
    response = client.delete(
        f"/api/v1/workspaces/{space}",
        headers={**ORIGIN, "X-CSRF-Token": identity["csrf_token"]},
    )
    assert response.status_code == 204
    assert client.get(f"/api/v1/workspaces/{space}").status_code == 404
    assert client.get("/api/v1/workspaces").json()["items"] == []
    assert client.get("/api/v1/auth/me").json()["workspaces"] == []
    member = login(client, "member@example.test")
    assert member["workspaces"] == []
    other = login(client, "other@example.test")
    assert len(other["workspaces"]) == 1


def test_workspace_delete_requires_owner_and_csrf(client: TestClient) -> None:
    """普通成员和缺少防伪令牌的所有者不能删除空间。"""
    identity = login(client, "member@example.test")
    space = identity["workspaces"][0]["id"]
    assert client.delete(f"/api/v1/workspaces/{space}", headers=ORIGIN).status_code == 403
    assert (
        client.delete(
            f"/api/v1/workspaces/{space}",
            headers={**ORIGIN, "X-CSRF-Token": identity["csrf_token"]},
        ).status_code
        == 403
    )


@pytest.fixture
def client() -> Iterator[TestClient]:
    """构建隔离测试应用、数据库与用户，结束后释放测试资源。"""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        for email, role in (
            ("owner@example.test", "owner"),
            ("member@example.test", "member"),
            ("other@example.test", "owner"),
        ):
            user = User(
                email=email,
                name=email.split("@")[0],
                password_hash=hasher.hash(PASSWORD),
                platform_admin=email.startswith("owner"),
            )
            db.add(user)
            db.flush()
            if role == "owner":
                space = Workspace(name=user.name + " 空间")
                db.add(space)
                db.flush()
            else:
                space = db.scalar(select(Workspace).where(Workspace.name == "owner 空间"))
                assert space is not None
            db.add(Membership(workspace_id=space.id, user_id=user.id, role=role))
        db.commit()
    settings = Settings(
        tool_http_allowed_hosts=[],
        tool_secrets={},
        amap_api_key="",
        app_env="test",
        cumulative_budget_enabled=True,
        task_budget_enabled=True,
        embedding_provider="mock",
        auth_trusted_origins=["http://testserver"],
        model_provider="mock",
        model_id="mock-v1",
        model_api_key="",
        model_base_url="",
    )
    with TestClient(
        create_app(settings, engine=engine, limiter=lambda address, email: None)
    ) as connection:
        yield connection


def login(client: TestClient, email: str = "owner@example.test") -> dict[str, object]:
    """验证凭证并轮换会话令牌，使用安全选项写入浏览器会话。"""
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}, headers=ORIGIN
    )
    assert response.status_code == 200
    return response.json()


def test_login_logout_revocation_and_token_storage(client: TestClient) -> None:
    """验证登录与退出的会话撤销，以及数据库仅保存令牌摘要。"""
    assert client.get("/api/v1/auth/me").status_code == 401
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.test", "password": PASSWORD},
        headers=ORIGIN,
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    body = response.json()
    raw = client.cookies.get("yugitalk_session")
    assert raw and raw not in response.text
    with Session(client.app.state.database) as db:
        stored = db.scalar(select(LoginSession))
        assert stored and stored.token_hash != raw
        assert db.scalar(select(User)).password_hash.startswith("$argon2id$")
    assert client.get("/api/v1/auth/me").json()["user"]["email"] == "owner@example.test"
    assert (
        client.post(
            "/api/v1/auth/logout", headers={**ORIGIN, "X-CSRF-Token": body["csrf_token"]}
        ).status_code
        == 204
    )
    client.cookies.set("yugitalk_session", raw)
    assert client.get("/api/v1/auth/me").status_code == 401


@pytest.mark.parametrize(
    "email,password", [("unknown@example.test", PASSWORD), ("owner@example.test", "wrong")]
)
def test_invalid_credentials_have_same_response(
    client: TestClient, email: str, password: str
) -> None:
    """验证未知账号和错误密码返回相同提示。"""
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}, headers=ORIGIN
    )
    assert response.status_code == 401
    assert password not in response.text and email not in response.text


def test_login_requires_trusted_origin(client: TestClient) -> None:
    """验证登录请求必须来自显式受信任的来源。"""
    for headers in ({}, {"Origin": "https://attacker.test"}):
        assert (
            client.post(
                "/api/v1/auth/login",
                json={"email": "owner@example.test", "password": PASSWORD},
                headers=headers,
            ).status_code
            == 403
        )


@pytest.mark.parametrize("password", ["a", "x" * 129, "密码 含空格 " * 64])
def test_login_accepts_short_and_long_passwords(client: TestClient, password: str) -> None:
    """真实哈希验证短密码及超过旧上限的密码，完整凭证不得截断或去除空格。"""
    with Session(client.app.state.database) as db:
        user = db.scalar(select(User).where(User.email == "owner@example.test"))
        assert user is not None
        user.password_hash = hasher.hash(password)
        db.commit()
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.test", "password": password},
        headers=ORIGIN,
    )
    assert response.status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 200
    incorrect = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.test", "password": password + "x"},
        headers=ORIGIN,
    )
    assert incorrect.status_code == 401


def test_empty_password_is_invalid_credentials(client: TestClient) -> None:
    """空登录密码按错误凭证返回，不使用长度校验泄露密码规则。"""
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.test", "password": ""},
        headers=ORIGIN,
    )
    assert response.status_code == 401


@pytest.mark.parametrize("password", ["", "a", "x" * 256])
def test_bootstrap_password_without_length_policy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, password: str
) -> None:
    """初始化允许短密码和长密码且可实际登录，空密码仍不能创建账号。"""
    engine = client.app.state.database
    monkeypatch.setattr(bootstrap, "load_settings", lambda: client.app.state.settings)
    monkeypatch.setattr(bootstrap, "create_database", lambda settings: engine)
    # CLI 正常负责释放自建连接；此处连接由测试应用共享，交还给夹具管理。
    monkeypatch.setattr(engine, "dispose", lambda: None)
    monkeypatch.setattr(bootstrap.sys, "stdin", StringIO(password + "\n"))
    monkeypatch.setattr(
        bootstrap.sys,
        "argv",
        ["bootstrap", "--email", "length@example.test", "--name", "长度验收", "--password-stdin"],
    )
    assert bootstrap.main() == (0 if password else 2)
    with Session(engine) as db:
        created = db.scalar(select(User).where(User.email == "length@example.test"))
        assert bool(created) == bool(password)
    if password:
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "length@example.test", "password": password},
            headers=ORIGIN,
        )
        assert response.status_code == 200


def test_csrf_and_cross_space_access(client: TestClient) -> None:
    """验证写操作需要防伪令牌，并阻止跨空间越权访问。"""
    identity = login(client)
    for headers in (
        ORIGIN,
        {"Origin": "https://attacker.test", "X-CSRF-Token": identity["csrf_token"]},
    ):
        assert (
            client.post(
                "/api/v1/workspaces", json={"name": "forbidden"}, headers=headers
            ).status_code
            == 403
        )
    spaces = client.get("/api/v1/workspaces").json()["items"]
    assert len(spaces) == 1
    with Session(client.app.state.database) as db:
        other = db.scalar(select(Workspace).where(Workspace.name == "other 空间"))
        assert other
        other_id = other.id
    assert client.get(f"/api/v1/workspaces/{other_id}").status_code == 404
    assert client.get(f"/api/v1/workspaces/{other_id}/members").status_code == 404


def test_member_cannot_manage_and_permission_revocation(client: TestClient) -> None:
    """验证普通成员不能管理空间，权限撤销立即生效。"""
    data = login(client, "member@example.test")
    space = data["workspaces"][0]["id"]
    assert client.get(f"/api/v1/workspaces/{space}").status_code == 200
    assert client.get(f"/api/v1/workspaces/{space}/members").status_code == 403
    assert client.get("/api/v1/admin/audit").status_code == 403
    with Session(client.app.state.database) as db:
        member = db.get(Membership, (space, data["user"]["id"]))
        db.delete(member)
        db.commit()
    assert client.get(f"/api/v1/workspaces/{space}").status_code == 404


def test_expiry_and_disabled_user(client: TestClient) -> None:
    """验证过期会话和被停用用户均失去访问权限。"""
    login(client)
    with Session(client.app.state.database) as db:
        session = db.scalar(select(LoginSession))
        assert session
        session.expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert client.get("/api/v1/auth/me").status_code == 401
    login(client)
    with Session(client.app.state.database) as db:
        user = db.scalar(select(User).where(User.email == "owner@example.test"))
        assert user
        user.active = False
        db.commit()
    assert client.get("/api/v1/auth/me").status_code == 401


def test_last_owner_and_audit(client: TestClient) -> None:
    """验证不能移除最后一位所有者，并保存相关审计记录。"""
    data = login(client)
    headers = {**ORIGIN, "X-CSRF-Token": data["csrf_token"]}
    path = f"/api/v1/workspaces/{data['workspaces'][0]['id']}/members/{data['user']['id']}"
    assert client.patch(path, json={"role": "member"}, headers=headers).status_code == 409
    assert client.delete(path, headers=headers).status_code == 409
    created = client.post("/api/v1/workspaces", json={"name": "新空间"}, headers=headers)
    assert created.status_code == 201
    with Session(client.app.state.database) as db:
        assert db.scalar(select(AuditEvent).where(AuditEvent.action == "workspace.create"))


def test_limiter_failure_is_not_bypassed(client: TestClient) -> None:
    """验证限流存储故障不会使登录绕过限制。"""

    def limited(address: str, email: str) -> None:
        """模拟频率限制存储故障，验证登录不会绕过保护。"""
        raise HTTPException(429, headers={"Retry-After": "900"})

    client.app.state.login_limiter = limited
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.test", "password": PASSWORD},
        headers=ORIGIN,
    )
    assert response.status_code == 429 and response.headers["retry-after"] == "900"


def test_new_login_rotates_and_revokes_old_cookie(client: TestClient) -> None:
    """验证重新登录轮换令牌并撤销旧会话。"""
    login(client)
    previous = client.cookies.get("yugitalk_session")
    login(client)
    assert client.cookies.get("yugitalk_session") != previous
    client.cookies.clear()
    client.cookies.set("yugitalk_session", previous)
    assert client.get("/api/v1/auth/me").status_code == 401


def test_production_cookie_not_sent_over_http(client: TestClient) -> None:
    # 此处只覆盖 Cookie 行为，生产配置完整校验由配置边界测试覆盖。
    """验证生产会话凭证不会通过非加密连接发送。"""
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"app_env": "production", "auth_trusted_origins": ["https://secure.test"]}
    )
    result = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.test", "password": PASSWORD},
        headers={"Origin": "https://secure.test"},
    )
    assert result.status_code == 200
    cookie = result.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert client.get("/api/v1/auth/me").status_code == 401


def test_redis_connection_failure_closes_login(client: TestClient) -> None:
    """验证共享存储连接失败时登录受到保护。"""
    from pydantic import SecretStr

    from app.identity.login_limit import RedisLoginLimiter

    settings = client.app.state.settings.model_copy(
        update={"redis_url": SecretStr("redis://127.0.0.1:1/0")}
    )
    client.app.state.login_limiter = RedisLoginLimiter(settings)
    result = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.test", "password": PASSWORD},
        headers=ORIGIN,
    )
    assert result.status_code == 503
    assert not result.headers.get("set-cookie")


def test_cursor_pagination_and_limits(client: TestClient) -> None:
    """验证列表游标连续性、分页大小与数量上限。"""
    data = login(client)
    headers = {**ORIGIN, "X-CSRF-Token": data["csrf_token"]}
    for name in ("分页一", "分页二"):
        assert (
            client.post("/api/v1/workspaces", json={"name": name}, headers=headers).status_code
            == 201
        )
    seen: set[str] = set()
    cursor = ""
    while True:
        page = client.get(f"/api/v1/workspaces?limit=1{cursor}").json()
        assert len(page["items"]) == 1
        key = page["items"][0]["id"]
        assert key not in seen
        seen.add(key)
        if page["next_cursor"] is None:
            break
        cursor = "&cursor=" + page["next_cursor"]
    assert len(seen) == 3
    assert client.get("/api/v1/workspaces?limit=101").status_code == 422
    assert client.get("/api/v1/workspaces?cursor=invalid").status_code == 422


def test_member_business_errors_preserve_existing_role(client: TestClient) -> None:
    """验证重复成员等业务错误不会覆盖已有角色。"""
    data = login(client)
    headers = {**ORIGIN, "X-CSRF-Token": data["csrf_token"]}
    path = f"/api/v1/workspaces/{data['workspaces'][0]['id']}/members"
    duplicate = client.post(
        path, json={"email": "owner@example.test", "role": "member"}, headers=headers
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "member_already_exists"
    missing = client.post(path, json={"email": "uninitialized@example.test"}, headers=headers)
    assert missing.status_code == 404
    assert missing.json()["code"] == "member_user_unavailable"
    members = client.get(path).json()["items"]
    assert (
        next(item for item in members if item["email"] == "owner@example.test")["role"] == "owner"
    )
    last_owner = client.delete(f"{path}/{data['user']['id']}", headers=headers)
    assert last_owner.status_code == 409
    assert last_owner.json()["code"] == "last_workspace_owner"

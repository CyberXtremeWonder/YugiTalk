"""工作空间默认、个人配置隔离、部署开放范围及目录浏览回归。"""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_identity import ORIGIN, login

from app.providers.client import ProviderFailure
from app.workspaces import service
from app.workspaces.models import UserWorkspace

pytest_plugins = ["test_identity"]
PATH = "/api/v1/me/workspace"


def identity(client: TestClient, email: str = "owner@example.test") -> tuple[str, dict[str, str]]:
    """返回隔离用户与写请求防伪头，不使用实际业务身份。"""
    actor = login(client, email)
    return str(actor["user"]["id"]), {**ORIGIN, "X-CSRF-Token": str(actor["csrf_token"])}


def expose(client: TestClient, root: Path) -> None:
    """仅向测试应用开放临时目录，不读取用户文件或改变部署配置。"""
    client.app.state.settings.workspace_allowed_roots = [str(root)]


def test_default_is_empty_and_get_does_not_create_configuration(client: TestClient) -> None:
    """匿名不能读写，首次登录保持空配置，读取不会创建记录。"""
    assert client.get(PATH).status_code == 401
    assert client.get(PATH + "/directories").status_code == 401
    assert client.put(PATH, json={"directory": "", "enabled": False}).status_code == 401
    identity(client)
    data = client.get(PATH).json()
    assert data["directory"] == "" and data["enabled"] is False
    assert data["revision"] == 0 and data["available"] is False
    assert data["allowed_roots"] == []
    assert "尚未导入" in data["configuration_note"]
    assert client.get(PATH + "/directories").json() == {
        "path": "", "parent": None, "items": [], "allowed_roots": [], "truncated": False,
    }
    with Session(client.app.state.database) as db:
        assert db.scalar(select(func.count()).select_from(UserWorkspace)) == 0


def test_disabled_directory_draft_needs_no_mount_and_cannot_enable(client: TestClient) -> None:
    """允许先保存本机地址，未开放时不得伪装成可读取或成功启用。"""
    _, headers = identity(client)
    directory = r"E:\用户资料\尚未挂载"
    response = client.put(PATH, json={"directory": directory, "enabled": False}, headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["directory"] == directory and data["revision"] == 1
    assert not data["available"] and not data["enabled"]
    response = client.put(PATH, json={"directory": directory, "enabled": True}, headers=headers)
    assert response.status_code == 422
    assert response.json()["code"] == "workspace_directory_denied"
    assert client.get(PATH).json()["revision"] == 1


def test_workspace_is_owned_by_current_user_and_write_requires_csrf(
    client: TestClient, tmp_path: Path,
) -> None:
    """当前身份决定设置归属，前端不能通过用户标识访问或更新其他配置。"""
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    expose(client, tmp_path)
    owner, headers = identity(client)
    body = {"directory": str(first), "enabled": True}
    assert client.put(PATH, json=body, headers=ORIGIN).status_code == 403
    saved = client.put(PATH, json=body, headers=headers)
    assert saved.status_code == 200
    assert saved.json()["directory"] == str(first.resolve())
    assert saved.json()["available"]
    member, headers = identity(client, "member@example.test")
    assert client.get(PATH, params={"user_id": owner}).json()["directory"] == ""
    assert client.put(PATH, json={**body, "user_id": owner}, headers=headers).status_code == 422
    assert client.put(PATH, json={"directory": str(second), "enabled": True},
                      headers=headers).status_code == 200
    identity(client)
    assert client.get(PATH).json()["directory"] == str(first.resolve())
    with Session(client.app.state.database) as db:
        assert service.get_workspace(db, owner).directory == str(first.resolve())
        assert service.get_workspace(db, member).directory == str(second.resolve())


@pytest.mark.parametrize("changes", [
    {"directory": "relative/path"}, {"directory": "."},
    {"directory": "/bad\x00path"}, {"directory": "/bad\npath"},
    {"directory": "/" + "x" * 2048}, {"directory": "", "enabled": True},
    {"directory": "", "enabled": "false"}, {"directory": "", "unknown": True},
])
def test_configuration_contract_rejects_unsafe_or_ambiguous_values(
    client: TestClient, changes: dict[str, Any],
) -> None:
    """配置草稿也保持有界且严格，空目录不能隐式启用当前工作路径。"""
    _, headers = identity(client)
    response = client.put(PATH, json={"enabled": False, **changes}, headers=headers)
    assert response.status_code == 422


def test_enabled_directory_must_exist_and_stay_inside_opened_root(
    client: TestClient, tmp_path: Path,
) -> None:
    """允许范围、实际存在性和目录类型都在启用前校验，失败不写配置。"""
    root, outside = tmp_path / "opened", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    file = root / "file.txt"
    file.write_text("fixture", encoding="utf-8")
    expose(client, root)
    _, headers = identity(client)
    for path, code in (
        (str(outside), "workspace_directory_denied"),
        (str(root / ".." / "outside"), "workspace_directory_denied"),
        (str(root / "missing"), "workspace_unavailable"),
        (str(file), "workspace_unavailable"),
    ):
        response = client.put(PATH, json={"directory": path, "enabled": True}, headers=headers)
        assert response.status_code == 422
        assert response.json()["code"] == code
    assert client.get(PATH).json()["directory"] == ""


def test_revision_disabled_and_changed_configuration_block_execution(
    client: TestClient, tmp_path: Path,
) -> None:
    """同值保存不改变版本，关闭或改目录后执行器不能复用旧许可。"""
    expose(client, tmp_path)
    user, headers = identity(client)
    settings = client.app.state.settings
    body = {"directory": str(tmp_path), "enabled": True}
    first = client.put(PATH, json=body, headers=headers).json()
    same = client.put(PATH, json=body, headers=headers).json()
    assert first["revision"] == same["revision"] == 1
    with Session(client.app.state.database) as db:
        assert service.resolve_workspace(db, user, settings, expected_revision=1) == (
            tmp_path.resolve(), 1,
        )
        disabled = client.put(PATH, json={**body, "enabled": False}, headers=headers).json()
        assert disabled["revision"] == 2
        assert service.get_workspace(db, user).enabled is False
        with pytest.raises(ProviderFailure, match="workspace_changed"):
            service.resolve_workspace(db, user, settings, expected_revision=1)
        with pytest.raises(ProviderFailure, match="workspace_disabled"):
            service.resolve_workspace(db, user, settings)
    assert client.put(PATH, json=body, headers=headers).json()["revision"] == 3
    cleared = client.put(PATH, json={"directory": "", "enabled": False}, headers=headers).json()
    assert cleared["revision"] == 4 and not cleared["available"]
    with Session(client.app.state.database) as db:
        with pytest.raises(ProviderFailure, match="workspace_not_configured"):
            service.resolve_workspace(db, user, settings)


def test_removed_directory_or_deployment_permission_is_rechecked(
    client: TestClient, tmp_path: Path,
) -> None:
    """目录删除或部署收窄开放范围后，已保存设置不会继续授予读取能力。"""
    root, selected = tmp_path / "root", tmp_path / "root" / "selected"
    selected.mkdir(parents=True)
    expose(client, root)
    user, headers = identity(client)
    client.put(PATH, json={"directory": str(selected), "enabled": True}, headers=headers)
    selected.rmdir()
    assert client.get(PATH).json()["available"] is False
    with Session(client.app.state.database) as db:
        with pytest.raises(ProviderFailure, match="workspace_unavailable"):
            service.resolve_workspace(db, user, client.app.state.settings)
    selected.mkdir()
    client.app.state.settings.workspace_allowed_roots = []
    with Session(client.app.state.database) as db:
        with pytest.raises(ProviderFailure, match="workspace_directory_denied"):
            service.resolve_workspace(db, user, client.app.state.settings)


def test_directory_browser_only_exposes_directories_within_opened_roots(
    client: TestClient, tmp_path: Path,
) -> None:
    """目录选择器不返回文件，不允许越界或通过父目录查看服务端其他路径。"""
    root = tmp_path / "opened"
    child = root / "notes"
    child.mkdir(parents=True)
    (root / "private.txt").write_text("not a directory", encoding="utf-8")
    expose(client, root)
    identity(client)
    index = client.get(PATH + "/directories").json()
    assert index["items"] == [{"name": root.name, "path": str(root.resolve())}]
    listing = client.get(PATH + "/directories", params={"path": str(root)}).json()
    assert listing["parent"] is None
    assert listing["items"] == [{"name": "notes", "path": str(child.resolve())}]
    assert client.get(PATH + "/directories", params={"path": str(child)}).json()["parent"] == (
        str(root.resolve())
    )
    for path in (str(root.parent), str(root / ".."), str(root / "bad\x00name")):
        response = client.get(PATH + "/directories", params={"path": path})
        assert response.status_code == 422
        assert response.json()["code"] == "workspace_directory_denied"
    assert client.get(PATH + "/directories", params={"path": "/" + "x" * 2048}).status_code == 422


def test_symlink_directory_cannot_escape_opened_roots(client, tmp_path: Path) -> None:
    """真实软链接只能指向开放范围内，目录设置和浏览都不能借此越界。"""
    root, outside = tmp_path / "opened", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前系统不允许创建软链接")
    expose(client, root)
    _, headers = identity(client)
    response = client.put(PATH, json={"directory": str(link), "enabled": True}, headers=headers)
    assert response.status_code == 422
    assert response.json()["code"] == "workspace_directory_denied"
    assert client.get(PATH + "/directories", params={"path": str(root)}).json()["items"] == []


def test_directory_listing_reports_limits_and_does_not_scan_unbounded_files(
    client, tmp_path: Path, monkeypatch,
) -> None:
    """列表只返回有界子目录，大量非目录条目也不能造成无界扫描。"""
    for index in range(service.DIRECTORY_LIMIT + 1):
        (tmp_path / f"dir-{index:03}").mkdir()
    expose(client, tmp_path)
    identity(client)
    listing = client.get(PATH + "/directories", params={"path": str(tmp_path)}).json()
    assert len(listing["items"]) == service.DIRECTORY_LIMIT
    assert listing["truncated"] is True
    monkeypatch.setattr(service, "SCAN_LIMIT", 2)
    limited = client.get(PATH + "/directories", params={"path": str(tmp_path)}).json()
    assert len(limited["items"]) == 2 and limited["truncated"] is True

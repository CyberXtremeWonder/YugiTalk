"""读取当前用户工作空间，并将可选目录限制在部署明确开放的范围内。"""

import os
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.providers.client import ProviderFailure
from app.workspaces.models import UserWorkspace

DIRECTORY_LIMIT = 100
SCAN_LIMIT = 1000

NOTES = {
    "workspace_not_configured": "尚未选择读取目录，工作空间默认关闭。",
    "workspace_disabled": "工作空间已关闭，不会读取目录中的文件。",
    "workspace_changed": "工作空间配置已变更，请重新创建任务。",
    "workspace_directory_denied": "目录不在服务端开放范围内，请从已开放目录中选择。",
    "workspace_unavailable": "目录不可访问，请确认目录存在且已向执行服务只读开放。",
}


def allowed_roots(settings: Settings) -> list[Path]:
    """只公开已开放且可读取的目录，不创建目录或展开任意环境变量。"""
    result = []
    for value in settings.workspace_allowed_roots:
        candidate = Path(value)
        if not candidate.is_absolute():
            continue
        try:
            root = candidate.resolve(strict=True)
            if root.is_dir() and os.access(root, os.R_OK | os.X_OK) and root not in result:
                result.append(root)
        except (OSError, RuntimeError, ValueError):
            continue
    return result


def directory_path(value: str, roots: list[Path]) -> Path:
    """先校验目录范围，再解析实际路径，软链接也不能扩展部署开放范围。"""
    if len(value) > 2048 or any(ord(character) < 32 for character in value):
        raise ProviderFailure("workspace_directory_denied")
    candidate = Path(value)
    if not candidate.is_absolute():
        raise ProviderFailure("workspace_directory_denied")
    candidate = Path(os.path.abspath(candidate))
    if not roots or not any(candidate.is_relative_to(root) for root in roots):
        raise ProviderFailure("workspace_directory_denied")
    try:
        target = candidate.resolve(strict=True)
        if not any(target.is_relative_to(root) for root in roots):
            raise ProviderFailure("workspace_directory_denied")
        if not target.is_dir() or not os.access(target, os.R_OK | os.X_OK):
            raise ProviderFailure("workspace_unavailable")
    except (OSError, RuntimeError):
        raise ProviderFailure("workspace_unavailable") from None
    return target


def get_workspace(db: Session, user_id: str) -> UserWorkspace | None:
    """只按服务端认证身份读取配置，不使用模型或前端提交的用户标识。"""
    return db.get(UserWorkspace, user_id, populate_existing=True)


def resolve_workspace(
    db: Session,
    user_id: str,
    settings: Settings,
    *,
    expected_revision: int | None = None,
) -> tuple[Path, int]:
    """执行及缓存复用前重查启停、配置版本和当前目录范围。"""
    row = get_workspace(db, user_id)
    if row is None or not row.directory:
        raise ProviderFailure("workspace_not_configured")
    if expected_revision is not None and row.revision != expected_revision:
        raise ProviderFailure("workspace_changed")
    if not row.enabled:
        raise ProviderFailure("workspace_disabled")
    return directory_path(row.directory, allowed_roots(settings)), row.revision


def workspace_info(db: Session, user_id: str, settings: Settings) -> dict[str, object]:
    """返回本人设置与实际可用状态，保存草稿不表示已经获得读取能力。"""
    row = get_workspace(db, user_id)
    directory = row.directory if row else ""
    enabled = row.enabled if row else False
    if row is not None and row.source == "imported":
        return {
            "directory": directory, "enabled": enabled, "revision": row.revision,
            "available": bool(row.files), "source": "imported",
            "file_count": len(row.files),
            "total_bytes": sum(item["size"] for item in row.files.values()),
            "imported_at": row.imported_at.isoformat() if row.imported_at else None,
            "files": [{"path": path, "size": item["size"]}
                      for path, item in sorted(row.files.items())],
            "configuration_note": (
                "已保存目录文件快照；本机文件更新后需重新导入。"
                if enabled else "目录快照已导入，启用后可读取；本机文件更新后需重新导入。"
            ),
            "allowed_roots": [],
        }
    roots = allowed_roots(settings)
    available = False
    if not roots:
        note = "尚未导入文件夹；可直接在网页选择本机目录并导入，无需挂载目录。"
    elif not directory:
        note = NOTES["workspace_not_configured"]
    else:
        try:
            directory_path(directory, roots)
            available = True
            note = (
                "目录已开放，可以读取其中的文本文件。" if enabled else NOTES["workspace_disabled"]
            )
        except ProviderFailure as error:
            note = NOTES[error.code]
    return {
        "directory": directory, "enabled": enabled, "revision": row.revision if row else 0,
        "available": available, "configuration_note": note,
        "allowed_roots": [str(root) for root in roots],
        "source": "server", "file_count": 0, "total_bytes": 0,
        "imported_at": None, "files": [],
    }


def list_directories(settings: Settings, value: str) -> dict[str, object]:
    """只列已开放目录的子目录，限制扫描数量并明确提示截断。"""
    roots = allowed_roots(settings)
    result: dict[str, object] = {
        "path": "", "parent": None, "items": [],
        "allowed_roots": [str(root) for root in roots], "truncated": False,
    }
    if not value:
        result["items"] = [{"name": root.name or str(root), "path": str(root)} for root in roots]
        return result
    current = directory_path(value, roots)
    parent = current.parent
    result["path"] = str(current)
    result["parent"] = str(parent) if any(parent.is_relative_to(root) for root in roots) else None
    items: list[dict[str, str]] = []
    try:
        for scanned, child in enumerate(current.iterdir()):
            if scanned >= SCAN_LIMIT:
                result["truncated"] = True
                break
            try:
                target = directory_path(str(child), roots)
            except ProviderFailure:
                continue
            if len(items) == DIRECTORY_LIMIT:
                result["truncated"] = True
                break
            items.append({"name": child.name, "path": str(target)})
    except OSError:
        raise ProviderFailure("workspace_unavailable") from None
    result["items"] = sorted(items, key=lambda item: item["name"].casefold())
    return result

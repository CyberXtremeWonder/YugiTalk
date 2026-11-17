"""将首次文件调用绑定到用户工作空间，恢复和缓存复用继续检查原配置。"""

from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.providers.client import ProviderFailure
from app.tasks.models import Run
from app.workspaces.models import UserWorkspace
from app.workspaces.service import get_workspace, resolve_workspace


def workspace_for_run(db: Session, run: Run, settings: Settings | None) -> Path | UserWorkspace:
    """只从任务所有者的服务端配置取目录，不让模型参数决定读取根目录。"""
    if settings is None:
        raise ProviderFailure("workspace_not_configured")
    pin = run.snapshot.get("file_workspace")
    if "file_workspace" in run.snapshot:
        if (
            not isinstance(pin, dict)
            or type(pin.get("revision")) is not int
            or not isinstance(pin.get("directory"), str)
        ):
            raise ProviderFailure("workspace_changed")
    else:
        pin = None
    row = get_workspace(db, run.owner_id)
    source = row.source if row is not None else "server"
    if pin is not None and pin.get("source", "server") != source:
        raise ProviderFailure("workspace_changed")
    if row is not None and source == "imported":
        if pin is not None and pin["revision"] != row.revision:
            raise ProviderFailure("workspace_changed")
        if not row.enabled:
            raise ProviderFailure("workspace_disabled")
        if not row.files:
            raise ProviderFailure("workspace_unavailable")
        root: Path | UserWorkspace = row
        directory, revision = row.directory, row.revision
    else:
        root, revision = resolve_workspace(
            db, run.owner_id, settings,
            expected_revision=pin["revision"] if pin is not None else None,
        )
        directory = str(root)
    if pin is not None and pin["directory"] != directory:
        raise ProviderFailure("workspace_changed")
    if pin is None:
        run.snapshot = {
            **run.snapshot, "file_workspace": {
                "directory": directory, "revision": revision,
                **({"source": source} if source == "imported" else {}),
            },
        }
    return root

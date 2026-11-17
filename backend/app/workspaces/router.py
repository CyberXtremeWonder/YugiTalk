"""当前用户工作空间配置与受限目录选择，不提供任意文件浏览接口。"""

from pathlib import Path, PureWindowsPath
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select

from app.audit.service import audit
from app.identity.dependencies import DB, Actor, Writer
from app.identity.models import User
from app.providers.client import ProviderFailure
from app.workspaces.contracts import WorkspaceInput
from app.workspaces.models import UserWorkspace
from app.workspaces.service import (
    allowed_roots,
    directory_path,
    get_workspace,
    list_directories,
    workspace_info,
)

router = APIRouter(prefix="/api/v1/me/workspace", tags=["工作空间"])


@router.get("")
def workspace(request: Request, actor: Actor, db: DB) -> dict[str, object]:
    """首次读取保持空目录且关闭，不创建任何目录或配置记录。"""
    return workspace_info(db, actor.user.id, request.app.state.settings)


@router.put("")
def save_workspace(
    data: WorkspaceInput, request: Request, actor: Writer, db: DB,
) -> dict[str, object]:
    """保存本人目录草稿或启用可访问目录，变更版本让旧任务失效。"""
    db.scalar(select(User).where(User.id == actor.user.id).with_for_update())
    settings = request.app.state.settings
    directory = data.directory
    row = get_workspace(db, actor.user.id)
    imported = row is not None and row.source == "imported" and directory == row.directory
    if directory and not imported and not (
        Path(directory).is_absolute() or PureWindowsPath(directory).is_absolute()
    ):
        raise HTTPException(422, detail={"code": "workspace_directory_denied"})
    if imported and data.enabled and row is not None and not row.files:
        raise HTTPException(422, detail={"code": "workspace_unavailable"})
    if data.enabled and not imported:
        try:
            directory = str(directory_path(directory, allowed_roots(settings)))
        except ProviderFailure as error:
            raise HTTPException(422, detail={"code": error.code}) from None
    old_manifest = (
        row.files if row is not None and row.source == "imported" and not imported else {}
    )
    if row is None:
        row = UserWorkspace(user_id=actor.user.id, directory=directory, enabled=data.enabled)
        db.add(row)
    elif row.directory != directory or row.enabled != data.enabled:
        row.directory, row.enabled = directory, data.enabled
        row.revision += 1
    if not imported:
        row.source, row.files, row.imported_at = "server", {}, None
    audit(db, actor.user, "workspace.configure", actor.user.id, actor.user.id)
    db.commit()
    if old_manifest:
        from app.rag.storage import ObjectStorage
        from app.workspaces.imports import remove_files

        try:
            remove_files(ObjectStorage(settings), old_manifest, actor.user.id)
        except Exception:
            # 存储暂不可用时已提交的关闭或清空仍然生效。
            pass
    return workspace_info(db, actor.user.id, settings)


@router.post("/import")
async def import_directory(request: Request, actor: Writer, db: DB) -> dict[str, object]:
    """从浏览器选定目录导入本人文件快照，不申请 Docker 或宿主文件系统权限。"""
    from app.workspaces.imports import import_workspace

    user_id = actor.user.id
    await import_workspace(request, db, user_id, request.app.state.settings)
    return workspace_info(db, user_id, request.app.state.settings)


@router.get("/directories")
def directories(
    request: Request, actor: Actor, path: Annotated[str, Query(max_length=2048)] = "",
) -> dict[str, object]:
    """目录选择沿用登录身份，仅浏览部署明确开放的只读目录。"""
    try:
        return list_directories(request.app.state.settings, path)
    except ProviderFailure as error:
        raise HTTPException(422, detail={"code": error.code}) from None

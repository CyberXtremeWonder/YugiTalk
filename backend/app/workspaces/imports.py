"""浏览器目录快照导入：限定上传内容，原子替换本人清单，不使用本机挂载。"""

import hashlib
import logging
import re
from collections.abc import AsyncGenerator
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.audit.service import audit
from app.core.config import Settings
from app.core.entities import now
from app.identity.models import User
from app.providers.client import ProviderFailure
from app.rag.storage import ObjectStorage
from app.tools.builtin.files import MAX_READ_BYTES, MAX_WORD_BYTES, content_text
from app.workspaces.models import UserWorkspace
from app.workspaces.service import get_workspace

MAX_IMPORT_FILES = 100
MAX_IMPORT_BYTES = 50 * 1024 * 1024
MAX_IMPORT_WIRE_BYTES = MAX_IMPORT_BYTES + 1024 * 1024
ALLOWED_SUFFIXES = {".txt", ".md", ".docx"}
logger = logging.getLogger(__name__)


class CompleteMultiPartParser(MultiPartParser):
    """要求完整结束边界；截断请求不能把已完成前缀当成完整目录。"""

    complete = False

    def on_end(self) -> None:
        """仅解析到真正的 multipart 结束边界后允许发布快照。"""
        self.complete = True

    async def parse(self) -> FormData:
        """EOF 截断时也关闭尚未进入 FormData 的临时文件。"""
        try:
            result = await super().parse()
            if not self.complete:
                raise MultiPartException("incomplete multipart")
            return result
        except BaseException:
            for file in self._files_to_close_on_error:
                file.close()
            raise


def relative_path(value: str) -> str:
    """上传名只能表示选定目录内的相对文件，且不隐式折叠任何路径段。"""
    if (
        not value or len(value) > 512
        or any(ord(char) < 32 for char in value)
        or "\\" in value or ":" in value or value.startswith("/")
        or PureWindowsPath(value).drive
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or PurePosixPath(value).suffix.lower() not in ALLOWED_SUFFIXES
    ):
        raise ProviderFailure("file_path_denied")
    return value


def folder_label(value: str) -> str:
    """浏览器只能提供目录显示名称，不把名称解释为服务器绝对路径。"""
    if (
        not value or len(value) > 160 or value != value.strip() or value in {".", ".."}
        or any(ord(char) < 32 or char in "/\\:" for char in value)
    ):
        raise ProviderFailure("file_path_denied")
    return value


def owned_key(value: object, user_id: str) -> bool:
    """即便数据库清单损坏，也不能读取或清理其他用户的对象。"""
    return isinstance(value, str) and bool(re.fullmatch(
        rf"workspaces/{re.escape(user_id)}/[a-f0-9]{{32}}/[a-f0-9]{{32}}", value,
    ))


def imported_file(workspace: UserWorkspace, relative: str) -> tuple[str, dict[str, Any]]:
    """工具按当前用户快照清单定位对象，模型不提供对象键或用户身份。"""
    path = relative_path(relative.replace("\\", "/"))
    entry = workspace.files.get(path)
    if entry is None:
        raise ProviderFailure("file_unavailable")
    if (
        not isinstance(entry, dict) or not owned_key(entry.get("key"), workspace.user_id)
        or type(entry.get("size")) is not int or not 0 <= entry["size"] <= MAX_WORD_BYTES
        or not isinstance(entry.get("sha256"), str)
        or re.fullmatch(r"[a-f0-9]{64}", entry["sha256"]) is None
    ):
        raise ProviderFailure("workspace_changed")
    return path, entry


def read_imported_file(
    workspace: UserWorkspace, relative: str, settings: Settings,
) -> dict[str, object]:
    """有界读取服务器保存的不可变快照，并校验内容摘要后复用文本解析。"""
    path, entry = imported_file(workspace, relative)
    try:
        content = ObjectStorage(settings).get(entry["key"])
    except Exception:
        raise ProviderFailure("file_unavailable") from None
    if len(content) != entry["size"] or hashlib.sha256(content).hexdigest() != entry["sha256"]:
        raise ProviderFailure("file_unavailable")
    return {"ok": True, "path": path, "content": content_text(
        content, PurePosixPath(path).suffix.lower(),
    )}


def remove_files(storage: ObjectStorage, manifest: dict[str, Any], user_id: str) -> None:
    """只清理本人清单中的对象，失败不撤销已经提交的新配置。"""
    for item in manifest.values():
        key = item.get("key") if isinstance(item, dict) else None
        if isinstance(key, str) and owned_key(key, user_id):
            try:
                storage.remove(key)
            except Exception:
                logger.warning("工作空间快照对象暂未清理")


async def parse_import(request: Request) -> FormData:
    """先认证再流式限量解析，多文件和无长度头请求都不能绕过总字节上限。"""
    too_large = False

    async def bounded() -> AsyncGenerator[bytes, None]:
        """在 MultipartParser 接收内容前限制整个请求并触发其临时文件清理。"""
        nonlocal too_large
        total = 0
        async for block in request.stream():
            total += len(block)
            if total > MAX_IMPORT_WIRE_BYTES:
                too_large = True
                raise MultiPartException("workspace import too large")
            yield block

    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data;"):
        raise HTTPException(422, detail={"code": "workspace_import_invalid"})
    parser = CompleteMultiPartParser(
        request.headers, bounded(), max_files=MAX_IMPORT_FILES, max_fields=1, max_part_size=4096,
    )
    try:
        return await parser.parse()
    except MultiPartException as error:
        limited = too_large or error.message.startswith("Too many files.")
        code = "workspace_import_limit" if limited else "workspace_import_invalid"
        raise HTTPException(413 if limited else 422, detail={"code": code}) from None


async def import_workspace(
    request: Request, db: Session, user_id: str, settings: Settings,
) -> None:
    """先完整验证与保存新对象，再在用户锁下替换清单；失败保留旧工作空间。"""
    db.rollback()
    form = await parse_import(request)
    staged: dict[str, Any] = {}
    storage: ObjectStorage | None = None
    old_manifest: dict[str, Any] = {}
    committed = False
    commit_attempted = False
    try:
        storage = ObjectStorage(settings)
        fields = form.multi_items()
        names = form.getlist("directory")
        uploads = form.getlist("files")
        if (
            any(name not in {"directory", "files"} for name, _ in fields)
            or len(names) != 1 or not isinstance(names[0], str)
            or not 1 <= len(uploads) <= MAX_IMPORT_FILES
            or not all(isinstance(file, UploadFile) for file in uploads)
        ):
            raise ProviderFailure("workspace_import_invalid")
        directory = folder_label(names[0])
        paths: set[str] = set()
        validated: list[tuple[str, UploadFile, dict[str, Any]]] = []
        total = 0
        import_id = uuid4().hex
        for file in uploads:
            assert isinstance(file, UploadFile)
            path = relative_path(file.filename or "")
            # Windows 上选择的目录不区分大小写，不接受同名覆盖或大小写歧义。
            if path.casefold() in paths:
                raise ProviderFailure("workspace_import_invalid")
            paths.add(path.casefold())
            suffix = PurePosixPath(path).suffix.lower()
            maximum = MAX_WORD_BYTES if suffix == ".docx" else MAX_READ_BYTES
            content = await file.read(maximum + 1)
            total += len(content)
            if total > MAX_IMPORT_BYTES:
                raise ProviderFailure("workspace_import_limit")
            await run_in_threadpool(content_text, content, suffix)
            entry: dict[str, Any] = {
                "key": f"workspaces/{user_id}/{import_id}/{uuid4().hex}",
                "size": len(content), "sha256": hashlib.sha256(content).hexdigest(),
            }
            validated.append((path, file, entry))
        for path, file, entry in validated:
            staged[path] = entry
            await file.seek(0)
            await run_in_threadpool(storage.put_file, entry["key"], file.file)
        user = db.scalar(select(User).where(User.id == user_id).with_for_update())
        if user is None or not user.active:
            raise HTTPException(401)
        row = get_workspace(db, user_id)
        if row is None:
            row = UserWorkspace(user_id=user_id, revision=1)
            db.add(row)
        else:
            old_manifest = row.files if row.source == "imported" else {}
            row.revision += 1
        row.source, row.directory, row.enabled = "imported", directory, False
        row.files, row.imported_at = staged, now()
        audit(db, user, "workspace.import", user_id, user_id)
        commit_attempted = True
        db.commit()
        committed = True
    except ProviderFailure as error:
        db.rollback()
        code = error.code
        if code not in {"workspace_import_limit", "file_too_large"}:
            code = "workspace_import_invalid"
        raise HTTPException(413 if code != "workspace_import_invalid" else 422,
                            detail={"code": code}) from None
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        code = "workspace_import_uncertain" if commit_attempted else "workspace_import_unavailable"
        raise HTTPException(503, detail={"code": code}) from None
    finally:
        await form.close()
        # 提交响应丢失时事务可能已成功，不能清理可能已被当前清单引用的新对象。
        if storage is not None and (committed or not commit_attempted):
            await run_in_threadpool(remove_files, storage,
                                    old_manifest if committed else staged, user_id)

"""工具文件交付适配：二进制留在附件存储，模型只接收元数据。"""

import base64
import hashlib
import io
import time
import zipfile
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.research.files import filename
from app.research.models import ResearchProject, ResearchReport
from app.tasks.models import Run
from app.tools.builtin.archives import CreateZip, archive

ARTIFACT_TOOLS = {"create_zip", "fetch_picture"}


def build_files(
    name: str, arguments: object, guard: Callable[[], None]
) -> list[tuple[str, str, bytes]]:
    from app.tools.http.pictures import fetch_picture, picture, read_url
    from app.tools.http.pictures_contract import FetchPicture

    if name == "fetch_picture":
        files = fetch_picture(arguments, guard)
        if FetchPicture.model_validate(arguments).as_zip:
            return [
                (
                    "图片.zip",
                    "application/zip",
                    archive([(name, content) for name, _, content in files]),
                )
            ]
        return files
    request = CreateZip.model_validate(arguments)
    entries = [(file.filename, file.content.encode("utf-8")) for file in request.files]
    deadline = time.monotonic() + 30
    for index, url in enumerate(request.image_urls, 1):
        data, _, _ = read_url(url, guard, deadline)
        image_name, _, content = picture(data, index)
        entries.append((image_name, content))
    guard()
    title = request.title[:-4] if request.title.lower().endswith(".zip") else request.title
    return [(filename(title, "zip"), "application/zip", archive(entries))]


def save_files(db: Session, run: Run, files: list[tuple[str, str, bytes]]) -> dict[str, object]:
    project = ResearchProject(
        owner_id=run.owner_id,
        title=files[0][0],
        question="工具文件交付",
        options={"artifact_tool": True},
        state={"phase": "completed"},
    )
    db.add(project)
    db.flush()
    stored = {}
    metadata = []
    for index, (name, media, content) in enumerate(files):
        key = "zip" if media == "application/zip" else f"picture-{index}"
        stored[key] = {
            "filename": name,
            "media_type": media,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "content": base64.b64encode(content).decode("ascii"),
        }
        file: dict[str, object] = {
            "filename": name,
            "size": len(content),
            "download_url": f"/api/v1/research/{project.id}/files/{key}",
        }
        if media == "application/zip":
            with zipfile.ZipFile(io.BytesIO(content)) as zipped:
                file["entries"] = zipped.namelist()
        metadata.append(file)
    db.add(
        ResearchReport(
            project_id=project.id, run_id=run.id, version=1, markdown="", data={"files": stored}
        )
    )
    return {"ok": True, "summary": "文件已生成，可从聊天附件下载", "files": metadata}

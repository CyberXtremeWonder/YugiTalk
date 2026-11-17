"""研究项目只允许所有者访问；来源和报告读取时复核文档权限与版本。"""

import base64
from typing import Annotated, Any, cast
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path
from sqlalchemy import select
from starlette.responses import Response

from app.identity.dependencies import DB, Actor, Writer
from app.rag.models import Document, KnowledgeBase
from app.research.contracts import ProjectInput
from app.research.models import ResearchProject, ResearchReport, ResearchSource
from app.tasks.access import owned_run
from app.tasks.models import Run
from app.tasks.reliability import retryable_run

router = APIRouter(prefix="/api/v1/research", tags=["深度研究"])


def attachments(db: DB, run_id: str, owner: str) -> list[dict[str, object]]:
    """聊天附件仅返回下载元数据，正文与文件内容由下载接口重新鉴权。"""
    rows = db.execute(
        select(ResearchProject, ResearchReport)
        .join(ResearchReport, ResearchReport.project_id == ResearchProject.id)
        .where(ResearchReport.run_id == run_id, ResearchProject.owner_id == owner)
    )
    result: list[dict[str, object]] = []
    for project, report in rows:
        available = True
        try:
            validate_sources(db, project)
        except HTTPException:
            available = False
        for format, file in report.data.get("files", {}).items():
            result.append(
                {
                    "filename": file["filename"],
                    "size": file["size"],
                    "available": available,
                    "download_url": f"/api/v1/research/{project.id}/files/{format}"
                    if available
                    else None,
                }
            )
    return result


@router.get("/{project_id}/files/{format}")
def download_file(
    project_id: UUID,
    format: Annotated[str, Path(pattern=r"^(docx|pdf|md|zip|picture-[0-5])$")],
    db: DB,
    actor: Actor,
) -> Response:
    """每次下载复核所有者及内部资料版本，文件名不得作为路径使用。"""
    project = owned_project(db, str(project_id), actor.user.id)
    validate_sources(db, project)
    report = db.scalar(select(ResearchReport).where(ResearchReport.project_id == project.id))
    file = report.data.get("files", {}).get(format) if report else None
    if not file:
        raise HTTPException(404)
    return Response(
        content=base64.b64decode(file["content"]),
        media_type=file["media_type"],
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''"
            + quote(file["filename"], safe=""),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/runs/{run_id}/progress")
def progress(run_id: UUID, db: DB, actor: Actor) -> dict[str, object]:
    """对话展示公开研究进度，不返回正文快照或内部模型控制输出。"""
    run = owned_run(db, str(run_id), actor)
    records = cast(dict[str, Any], run.checkpoint.get("research_tools", {}))
    keys = {row["project_id"] for row in records.values()}
    if run.snapshot.get("research_project_id"):
        keys.add(str(run.snapshot["research_project_id"]))
    rows = db.scalars(
        select(ResearchProject).where(
            ResearchProject.id.in_(keys), ResearchProject.owner_id == actor.user.id
        )
    )
    return {
        "items": [
            {
                "title": p.title,
                "current_step": p.state.get("current_step"),
                "plan": p.state.get("plan"),
                "phase": p.state.get("phase"),
            }
            for p in rows
        ],
        "status": run.status,
    }


def owned_project(db: DB, key: str, owner: str, lock: bool = False) -> ResearchProject:
    query = select(ResearchProject).where(
        ResearchProject.id == key, ResearchProject.owner_id == owner
    )
    row = db.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise HTTPException(404)
    return row


def validate_sources(db: DB, project: ResearchProject) -> None:
    for row in db.scalars(select(ResearchSource).where(ResearchSource.project_id == project.id)):
        source = row.data
        if source.get("kind") != "knowledge":
            continue
        doc = db.get(Document, source["document_id"])
        base = db.get(KnowledgeBase, source["knowledge_base_id"])
        if (
            not doc
            or not base
            or base.deleted
            or base.account_id != project.owner_id
            or doc.account_id != project.owner_id
            or doc.knowledge_base_id != base.id
            or doc.status != "ready"
            or doc.index_version != source["index_version"]
            or doc.embedding_version != source["embedding_version"]
            or doc.collection_name != source["collection_name"]
        ):
            raise HTTPException(409, detail={"code": "research_source_unavailable"})


@router.post("", status_code=201)
def create_project(data: ProjectInput, db: DB, actor: Writer) -> dict[str, object]:
    for key in data.knowledge_base_ids:
        base = db.get(KnowledgeBase, str(key))
        if not base or base.deleted or base.account_id != actor.user.id:
            raise HTTPException(404)
    if not data.web_enabled and not data.knowledge_base_ids:
        raise HTTPException(422, detail="至少选择一种资料来源")
    row = ResearchProject(
        owner_id=actor.user.id,
        title=data.title,
        question=data.question,
        options=data.model_dump(mode="json", exclude={"title", "question"}),
        state={},
    )
    db.add(row)
    db.commit()
    return {"id": row.id, "title": row.title, "state": row.state}


@router.get("")
def projects(db: DB, actor: Actor) -> dict[str, object]:
    rows = db.scalars(
        select(ResearchProject)
        .where(ResearchProject.owner_id == actor.user.id)
        .order_by(ResearchProject.created_at.desc())
        .limit(100)
    )
    return {"items": [{"id": r.id, "title": r.title} for r in rows]}


@router.get("/{project_id}")
def project(project_id: UUID, db: DB, actor: Actor) -> dict[str, object]:
    row = owned_project(db, str(project_id), actor.user.id)
    validate_sources(db, row)
    run = db.get(Run, row.state.get("run_id", ""))
    return {
        "id": row.id,
        "title": row.title,
        "question": row.question,
        "options": row.options,
        "state": row.state,
        "run": {
            "id": run.id,
            "status": run.status,
            "error_code": run.error_code,
            "retryable": retryable_run(run),
        }
        if run
        else None,
        "sources": [
            {"id": s.id, **s.data}
            for s in db.scalars(
                select(ResearchSource)
                .where(ResearchSource.project_id == row.id)
                .order_by(ResearchSource.created_at, ResearchSource.id)
            )
        ],
        "reports": [
            {
                "version": r.version,
                "markdown": r.markdown,
                "data": {k: v for k, v in r.data.items() if k != "files"},
            }
            for r in db.scalars(
                select(ResearchReport)
                .where(ResearchReport.project_id == row.id)
                .order_by(ResearchReport.version.desc())
            )
        ],
    }

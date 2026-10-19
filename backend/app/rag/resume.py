"""知识库澄清入口：同一任务、状态版本校验、幂等提交和权限复核。"""

import hashlib
import json
from datetime import datetime
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import func, select

from app.chat.models import Message
from app.core.entities import now
from app.identity.dependencies import DB, Writer
from app.identity.models import User
from app.rag.contracts import ClarificationInput
from app.rag.models import Document
from app.tasks.access import owned_run, run_data
from app.tasks.execution import append_event
from app.tasks.models import Run
from app.tools.service import check_resume_tools

router = APIRouter(prefix="/api/v1", tags=["知识库澄清"])


@router.post("/runs/{run_id}/clarify", status_code=202)
def clarify(
    run_id: UUID,
    data: ClarificationInput,
    db: DB,
    actor: Writer,
    request: Request,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> dict[str, object]:
    """恢复已保存的 RAG 阶段，保留任务身份和次数；补充消息只写入一次。"""
    previous = owned_run(db, str(run_id), actor)
    db.scalar(select(User).where(User.id == previous.account_id).with_for_update())
    db.scalar(select(User).where(User.id == actor.user.id).with_for_update())
    run = db.scalar(
        select(Run)
        .where(Run.id == previous.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert run
    fingerprint = hashlib.sha256(
        json.dumps(data.model_dump(), sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    receipt = cast(dict[str, Any], run.snapshot.get("rag_resume_receipt", {}))
    if receipt.get("key") == idempotency_key:
        if receipt.get("hash") != fingerprint:
            raise HTTPException(409)
        return run_data(run)
    if (
        run.status != "waiting_input"
        or run.state_version != data.state_version
        or not data.answer.strip()
    ):
        raise HTTPException(409, detail={"code": "rag_resume_conflict"})
    pause = cast(dict[str, Any], run.snapshot.get("rag_clarification", {}))
    from app.skills.service import check_resume_skill

    check_resume_skill(db, run.snapshot, run.account_id)
    check_resume_tools(db, run.account_id, run.owner_id, run.snapshot, request.app.state.settings)
    if not pause.get("expires_at") or datetime.fromisoformat(pause["expires_at"]) <= now():
        raise HTTPException(410, detail={"code": "rag_clarification_expired"})
    state = cast(dict[str, Any], run.checkpoint.get("rag", {}))
    if (
        state.get("phase") != "clarify"
        or state.get("clarification_count", 0) >= 1
        or state.get("grade_count", 0) >= 2
    ):
        raise HTTPException(409, detail={"code": "rag_checkpoint_invalid"})
    for source in cast(list[dict[str, Any]], state.get("sources", [])):
        doc = db.get(Document, source["document_id"])
        if (
            not doc
            or doc.account_id != run.account_id
            or doc.knowledge_base_id != run.snapshot.get("knowledge_base_id")
            or doc.status != "ready"
            or doc.index_version != source["index_version"]
            or doc.collection_name != source["collection_name"]
        ):
            raise HTTPException(409, detail={"code": "knowledge_source_removed"})
    if request.app.state.settings.task_budget_enabled and run.charged_tokens >= run.reserved_tokens:
        raise HTTPException(429, detail={"code": "run_budget_insufficient"})
    question = str(state["question"])[:1000] + "\n补充：" + data.answer.strip()
    run.checkpoint = {
        "node": "rag",
        "rag": {
            **state,
            "question": question,
            "queries": [question],
            "phase": "retrieve",
            "sources": [],
            "clarification_count": 1,
        },
    }
    run.snapshot = {
        **run.snapshot,
        "sources": [],
        "rag_resume_receipt": {"key": idempotency_key, "hash": fingerprint},
    }
    run.status, run.cancel_requested, run.last_dispatched_at = "queued", False, None
    run.state_version += 1
    sequence = (
        db.scalar(
            select(func.max(Message.sequence)).where(Message.conversation_id == run.conversation_id)
        )
        or 0
    ) + 1
    db.add(
        Message(
            conversation_id=run.conversation_id,
            sequence=sequence,
            role="user",
            content=data.answer.strip(),
        )
    )
    append_event(db, run, "tool_status", {"stage": "rag_resume", "summary": "补充信息后恢复原任务"})
    db.commit()
    result = run_data(run)
    try:
        request.app.state.dispatch_run(run.id)
    except Exception:
        # queued 已持久化，仍由原调度器补投，网络重试不新建任务。
        pass
    return result

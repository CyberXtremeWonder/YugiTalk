"""任务所有权校验、公开响应与状态集合。"""

from typing import Any, cast

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.accounts.access import membership
from app.identity.dependencies import Principal
from app.tasks.models import Run
from app.tasks.reliability import RETRYABLE

ACTIVE = ("queued", "running", "waiting_approval", "waiting_input")
TERMINAL = ("succeeded", "failed", "cancelled")


def owned_run(db: Session, key: str, actor: Principal) -> Run:
    """校验任务所有权及当前空间权限，禁止跨用户读取。"""
    run = db.get(Run, key)
    if run is None or run.owner_id != actor.user.id:
        raise HTTPException(404)
    membership(db, run.account_id, actor.user.id)
    return run


def run_data(run: Run) -> dict[str, object]:
    """返回任务公开状态、用量及恢复信息，隐藏内部检查点内容。"""
    pin = cast(dict[str, Any] | None, run.snapshot.get("skill"))
    return {
        "id": run.id,
        "status": run.status,
        "conversation_id": run.conversation_id,
        "agent_version_id": run.agent_version_id,
        "knowledge_base_id": run.snapshot.get("knowledge_base_id"),
        "error_code": run.error_code,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "charged_tokens": run.charged_tokens,
        "reserved_tokens": run.reserved_tokens,
        "model": run.snapshot.get("model"),
        "provider": run.snapshot.get("provider"),
        "cost": None,
        "cost_note": "未配置价格版本，费用不可估算",
        "events_url": f"/api/v1/runs/{run.id}/events",
        "state_version": run.state_version,
        "event_sequence": run.event_sequence,
        "attempts": run.attempts,
        "checkpoint_node": run.checkpoint.get("node"),
        "rag_trace": run.snapshot.get("rag_trace", []),
        "clarification": run.snapshot.get("rag_clarification")
        if run.status == "waiting_input"
        else None,
        "skill": {key: pin[key] for key in ("name", "number", "version_id", "content_hash")}
        if pin
        else None,
        "retryable": run.status == "failed" and run.error_code in RETRYABLE,
        "recovery_note": "从已提交检查点恢复；中断的模型调用可能再次计费，已成功的工具不会重放",
    }

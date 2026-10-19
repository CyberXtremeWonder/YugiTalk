"""用户提交只读工具、平台管理员审核；批准时重新验证声明与空间配额。"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import Field, model_validator
from sqlalchemy import func, select

from app.accounts.access import AccountId, membership
from app.audit.service import audit
from app.core.entities import now
from app.identity.contracts import InputModel
from app.identity.dependencies import DB, Actor, Writer
from app.identity.models import User
from app.identity.router import development_admin
from app.tools.management_contracts import ToolInput
from app.tools.models import ManagedTool, ToolSubmission
from app.tools.registry import TOOL_SCHEMAS
from app.tools.router import lock_workspace, publish
from app.tools.service import digest, find_tool

router = APIRouter(prefix="/api/v1", tags=["工具审核"])


class ReviewInput(InputModel):
    """审核只允许批准或驳回，驳回必须给出有界意见。"""

    decision: Literal["approved", "rejected"]
    reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def check_reason(self) -> "ReviewInput":
        """规范审核意见并拒绝没有理由的驳回。"""
        self.reason = self.reason.strip()
        if self.decision == "rejected" and not self.reason:
            raise ValueError("驳回必须填写审核意见")
        return self


def public_submission(row: ToolSubmission) -> dict[str, object]:
    """只返回声明与审核元数据，不含真实密钥或运行凭证。"""
    return {
        "id": row.id,
        "account_id": row.account_id,
        "config": row.config,
        "status": row.status,
        "reason": row.reason,
        "submitter_id": row.submitter_id,
        "created_at": row.created_at.isoformat(),
    }


@router.post("/me/tool-submissions", status_code=201)
def submit_tool(account_id: AccountId, data: ToolInput, db: DB, actor: Writer) -> dict[str, object]:
    """任何当前成员可提交新工具，只保存待审记录，不创建可调用版本。"""
    key = str(account_id)
    membership(db, key, actor.user.id)
    lock_workspace(db, key)
    if data.name in TOOL_SCHEMAS or find_tool(db, key, data.name):
        raise HTTPException(409, detail={"code": "tool_name_conflict"})
    count = (
        db.scalar(
            select(func.count())
            .select_from(ToolSubmission)
            .where(
                ToolSubmission.account_id == key,
                ToolSubmission.submitter_id == actor.user.id,
                ToolSubmission.status == "pending",
            )
        )
        or 0
    )
    if count >= 20:
        raise HTTPException(409, detail={"code": "tool_submission_limit"})
    config = data.model_dump(exclude={"enabled"})
    row = ToolSubmission(
        account_id=key, submitter_id=actor.user.id, config=config, content_hash=digest(config)
    )
    db.add(row)
    db.flush()
    audit(db, actor.user, "tool.submit", key, row.id)
    db.commit()
    return public_submission(row)


@router.get("/me/tool-submissions")
def own_submissions(
    account_id: AccountId,
    db: DB,
    actor: Actor,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: UUID | None = None,
) -> dict[str, object]:
    """用户只能查看自己在当前成员空间的提交及审核意见，支持有界分页。"""
    membership(db, str(account_id), actor.user.id)
    rows = db.scalars(
        select(ToolSubmission)
        .where(
            ToolSubmission.account_id == str(account_id),
            ToolSubmission.submitter_id == actor.user.id,
            ToolSubmission.id > (str(cursor) if cursor else ""),
        )
        .order_by(ToolSubmission.id)
        .limit(limit + 1)
    ).all()
    return {
        "items": [public_submission(row) for row in rows[:limit]],
        "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
    }


@router.get("/admin/tool-submissions")
def pending_submissions(
    db: DB,
    actor: Actor,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: UUID | None = None,
) -> dict[str, object]:
    """开发模式管理员分页查看全平台待审声明，页面加载不会调用工具网络。"""
    development_admin(actor)
    rows = db.scalars(
        select(ToolSubmission)
        .where(
            ToolSubmission.status == "pending", ToolSubmission.id > (str(cursor) if cursor else "")
        )
        .order_by(ToolSubmission.id)
        .limit(limit + 1)
    ).all()
    return {
        "items": [public_submission(row) for row in rows[:limit]],
        "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
    }


@router.post("/admin/tool-submissions/{submission_id}/review")
def review_tool(submission_id: UUID, data: ReviewInput, db: DB, actor: Writer) -> dict[str, object]:
    """仅审核待审状态，批准与新版本发布同事务，重验提交者有效身份和空间授权。"""
    development_admin(actor)
    row = db.get(ToolSubmission, str(submission_id))
    if row is None:
        raise HTTPException(404)
    key = row.account_id
    lock_workspace(db, key)
    row = db.scalar(
        select(ToolSubmission).where(ToolSubmission.id == str(submission_id)).with_for_update()
    )
    assert row is not None
    if row.status != "pending":
        raise HTTPException(409, detail={"code": "tool_submission_reviewed"})
    if data.decision == "approved":
        submitter = db.get(User, row.submitter_id)
        if not submitter or not submitter.active:
            raise HTTPException(409, detail={"code": "tool_submitter_unavailable"})
        membership(db, key, submitter.id)
        if digest(row.config) != row.content_hash:
            raise HTTPException(409, detail={"code": "tool_content_changed"})
        config = ToolInput.model_validate(row.config)
        if config.name in TOOL_SCHEMAS or find_tool(db, key, config.name):
            raise HTTPException(409, detail={"code": "tool_name_conflict"})
        count = (
            db.scalar(
                select(func.count())
                .select_from(ManagedTool)
                .where(
                    ManagedTool.account_id == key,
                    ManagedTool.deleted.is_(False),
                    ManagedTool.builtin.is_(False),
                )
            )
            or 0
        )
        if count >= 100:
            raise HTTPException(409, detail={"code": "tool_catalog_limit"})
        tool = ManagedTool(account_id=key, name=config.name, owner_id=row.submitter_id)
        db.add(tool)
        db.flush()
        publish(db, tool, config)
    row.status, row.reason = data.decision, data.reason
    row.reviewer_id, row.reviewed_at = actor.user.id, now()
    audit(db, actor.user, "tool.review." + data.decision, key, row.id)
    db.commit()
    return public_submission(row)

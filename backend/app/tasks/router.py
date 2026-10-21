"""任务提交、取消、检查点重试及公开事件流。"""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from app.accounts.access import AccountId, membership
from app.agents.contracts import effective_config
from app.agents.model_config import model_settings
from app.agents.models import Agent, AgentVersion
from app.audit.service import audit
from app.chat.access import owned_conversation
from app.chat.models import Conversation, Message
from app.core.config import Settings
from app.core.entities import now
from app.core.pagination import Limit, page
from app.identity.dependencies import DB, Actor, Writer, authenticated
from app.identity.models import User
from app.providers.client import ProviderFailure
from app.tasks.access import ACTIVE, TERMINAL, owned_run, run_data
from app.tasks.contracts import RunInput
from app.tasks.execution import append_event
from app.tasks.ledger_models import RunRetry
from app.tasks.models import Budget, Run, RunEvent
from app.tasks.reliability import RETRYABLE
from app.tools.service import check_resume_tools, freeze_tools

router = APIRouter(prefix="/api/v1", tags=["任务"])


@router.post("/runs", status_code=202)
def create_run(
    data: RunInput,
    request: Request,
    db: DB,
    actor: Writer,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> dict[str, object]:
    """原子校验幂等键、权限、并发和预算，保存消息并提交排队意图。"""
    key = str(data.conversation_id)
    conversation = owned_conversation(db, key, actor)
    if not data.message.strip():
        raise HTTPException(422)
    settings: Settings = request.app.state.settings
    # 空间、用户、会话按固定顺序加锁；预算行首次创建也由父行锁串行保护。
    db.scalar(select(User).where(User.id == conversation.account_id).with_for_update())
    db.scalar(select(User).where(User.id == actor.user.id).with_for_update())
    db.scalar(select(Conversation).where(Conversation.id == key).with_for_update())
    db.expire_all()
    conversation = owned_conversation(db, key, actor)
    fingerprint = hashlib.sha256(
        json.dumps(data.model_dump(mode="json"), sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    # 幂等键识别同一次提交，内容哈希确认重发没有换参数；命中后不再保存消息或预留预算。
    previous = db.scalar(
        select(Run).where(Run.owner_id == actor.user.id, Run.idempotency_key == idempotency_key)
    )
    if previous:
        if previous.request_hash != fingerprint:
            raise HTTPException(409)
        return run_data(previous)
    version = db.get(AgentVersion, str(data.agent_version_id))
    agent = db.scalar(
        select(Agent).where(Agent.id == version.agent_id).with_for_update()
    ) if version else None
    if (
        version is None or agent is None or agent.deleted
        or agent.account_id != conversation.account_id
    ):
        raise HTTPException(404)
    # 将已发布助手版本及本次选用的资源冻结为任务快照，后续编辑不会改变本次执行配置。
    config = effective_config(version.config)
    if data.skill_version_id:
        from app.skills.service import freeze_skill

        pin = freeze_skill(db, str(data.skill_version_id), conversation.account_id, actor.user.id)
        config["skill"] = pin
        # Skill 只会收窄助手工具上限，空许可集合不是全部放行。
        config["tools"] = [tool for tool in config["tools"] if tool in pin["allowed_tools"]]
    config["tool_catalog"] = freeze_tools(
        db, conversation.account_id, actor.user.id, config["tools"], settings
    )
    config["tools"] = list(config["tool_catalog"])
    # 新对话由 Router 委派执行助手；显式指定知识库的旧客户端保留原路径。
    config["router_mode"] = not bool(data.knowledge_base_id)
    if config["router_mode"]:
        from app.rag.contracts import freeze_rag_config
        from app.rag.embeddings import Embeddings

        config["rag_config"] = freeze_rag_config(settings)
        config["rag_embedding_version"] = Embeddings(settings).version
    if data.knowledge_base_id:
        from app.rag.models import KnowledgeBase

        base = db.get(KnowledgeBase, str(data.knowledge_base_id))
        if base is None or base.account_id != conversation.account_id:
            raise HTTPException(404)
        config["knowledge_base_id"] = base.id
        from app.rag.contracts import freeze_rag_config

        config["rag_config"] = freeze_rag_config(settings)
        from app.rag.embeddings import Embeddings

        config["rag_embedding_version"] = Embeddings(settings).version
    try:
        connection_settings = model_settings(settings, config)
    except ProviderFailure as error:
        raise HTTPException(409, detail={"code": str(error)}) from error
    if connection_settings.model_provider != "mock" and not (
        connection_settings.model_api_key.get_secret_value() and connection_settings.model_base_url
    ):
        raise HTTPException(503)
    # 同一会话不能同时生成两轮回复；此外还要分别检查空间和用户的并发上限。
    active = select(Run).where(Run.status.in_(ACTIVE))
    if db.scalar(active.where(Run.conversation_id == key).limit(1)):
        raise HTTPException(409)
    for column, value, cap in (
        (Run.account_id, conversation.account_id, settings.workspace_concurrency),
        (Run.owner_id, actor.user.id, settings.user_concurrency),
    ):
        count = (
            db.scalar(select(func.count()).select_from(active.where(column == value).subquery()))
            or 0
        )
        if count >= cap:
            raise HTTPException(429, detail={"code": "run_concurrency_exceeded"})
    # 接受任务时先占用预算，结束时 finish_run 再释放预留并结算费用。
    # 此处与消息、任务共用一个事务，任何校验失败都会一起回滚。
    reservation = int(str(config["token_budget"]))
    for budget_key, limit in (
        ("space:" + conversation.account_id, settings.workspace_token_budget),
        ("user:" + actor.user.id, settings.user_token_budget),
    ):
        budget = db.scalar(select(Budget).where(Budget.key == budget_key).with_for_update())
        if budget is None:
            budget = Budget(key=budget_key, reserved=0, spent=0)
            db.add(budget)
        if (
            settings.cumulative_budget_enabled
            and budget.spent + budget.reserved + reservation > limit
        ):
            raise HTTPException(429, detail={"code": "run_budget_insufficient"})
        budget.reserved += reservation
    # 用户消息先进入会话历史，Worker 的 build_messages 随后从这里组装模型上下文。
    sequence = (
        db.scalar(select(func.max(Message.sequence)).where(Message.conversation_id == key)) or 0
    ) + 1
    db.add(Message(conversation_id=key, sequence=sequence, role="user", content=data.message))
    run = Run(
        account_id=conversation.account_id,
        owner_id=actor.user.id,
        conversation_id=key,
        agent_version_id=version.id,
        snapshot=config,
        idempotency_key=idempotency_key,
        request_hash=fingerprint,
        prompt=data.message,
        reserved_tokens=reservation,
    )
    db.add(run)
    db.flush()
    audit(db, actor.user, "run.create", run.account_id, run.id)
    db.commit()
    result = run_data(run)
    # 必须先提交数据库再投递队列，否则 Worker 可能抢先读取到尚未提交的任务。
    try:
        request.app.state.dispatch_run(run.id)
    except Exception:
        # 数据库 queued 状态已经提交，调度器会补投；不能丢失已经接受的任务。
        pass
    return result


@router.get("/runs/{run_id}")
def get_run(run_id: UUID, db: DB, actor: Actor) -> dict[str, object]:
    """验证所有权后返回指定任务的公开状态。"""
    return run_data(owned_run(db, str(run_id), actor))


@router.get("/conversations/{conversation_id}/runs")
def conversation_runs(conversation_id: UUID, db: DB, actor: Actor) -> dict[str, object]:
    """验证对话所有权后返回最近的执行任务。"""
    key = str(conversation_id)
    owned_conversation(db, key, actor)
    rows = db.scalars(
        select(Run).where(Run.conversation_id == key).order_by(Run.created_at.desc()).limit(20)
    )
    return {"items": [run_data(row) for row in rows]}


@router.get("/me/runs")
def workspace_runs(
    account_id: AccountId, db: DB, actor: Actor, limit: Limit = 20, cursor: UUID | None = None
) -> dict[str, object]:
    """分页返回当前用户在指定空间内的执行任务。"""
    key = str(account_id)
    membership(db, key, actor.user.id)
    rows = db.scalars(
        select(Run)
        .where(
            Run.account_id == key,
            Run.owner_id == actor.user.id,
            Run.id > (str(cursor) if cursor else ""),
        )
        .order_by(Run.id)
        .limit(limit + 1)
    )
    return page([run_data(row) for row in rows], limit)


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: UUID, db: DB, actor: Writer, request: Request) -> dict[str, object]:
    """验证任务归属，提交取消标记并安全结算任务。"""
    key = str(run_id)
    owned_run(db, key, actor)
    run = db.scalar(
        select(Run).where(Run.id == key).with_for_update().execution_options(populate_existing=True)
    )
    assert run is not None
    if run.status in ACTIVE:
        run.cancel_requested = True
        db.commit()
        from app.tasks.execution import finish_run

        finish_run(
            request.app.state.database,
            key,
            "cancelled",
            "cancelled",
            "",
            run.charged_tokens,
            run.input_tokens,
            run.output_tokens,
        )
        db.expire_all()
        run = owned_run(db, key, actor)
    return run_data(run)


def poll_events(
    engine: Engine, request: Request, key: str, last: int, settings: Settings
) -> tuple[list[dict[str, object]], bool]:
    """重新校验会话和任务权限，读取游标后的事件及终态标记。"""
    with Session(engine) as db:
        actor = authenticated(request, db)
        run = owned_run(db, key, actor)
        created = (
            run.created_at.replace(tzinfo=UTC) if run.created_at.tzinfo is None else run.created_at
        )
        if created + timedelta(seconds=settings.run_event_retention_seconds) < now():
            raise HTTPException(410)
        rows = db.scalars(
            select(RunEvent)
            .where(RunEvent.run_id == key, RunEvent.sequence > last)
            .order_by(RunEvent.sequence)
            .limit(100)
        ).all()
        events = [
            {
                "run_id": key,
                "event_id": row.id,
                "sequence": row.sequence,
                "type": row.kind,
                "payload": row.payload,
            }
            for row in rows
        ]
        # 即使任务已结束，也要把剩余事件读完；每批最多 100 条，可能需要多次轮询。
        return events, run.status in (*TERMINAL, "waiting_input") and (
            not rows or rows[-1].sequence == run.event_sequence
        )


@router.get("/runs/{run_id}/events")
def events(
    run_id: UUID,
    request: Request,
    db: DB,
    actor: Actor,
    last_event_id: Annotated[str | None, Header()] = None,
    types: Annotated[str | None, Query(max_length=240)] = None,
) -> StreamingResponse:
    """验证事件游标和保留期后建立可续传的公开事件流。"""
    key = str(run_id)
    run = owned_run(db, key, actor)
    last = 0
    # 游标由任务 ID 和序号组成，既限制读取位置，也避免把另一任务的游标用到这里。
    if last_event_id:
        try:
            prefix, suffix = last_event_id.split(":")
            last = int(suffix)
            if prefix != key or last < 0 or last > run.event_sequence:
                raise ValueError
        except ValueError:
            raise HTTPException(422) from None
    settings: Settings = request.app.state.settings
    selected = set(types.split(",")) if types else None
    allowed = {
        "text_delta", "tool_status", "checkpoint_restored", "completed", "error", "clarification",
    }
    if selected is not None:
        if not selected <= allowed:
            raise HTTPException(422)
        # 终态和恢复事件必须送达，订阅者不能过滤掉决定结束或替换正文的控制事件。
        selected |= {"completed", "error", "clarification", "checkpoint_restored"}
    engine: Engine = request.app.state.database
    # 在发出响应头前校验过期游标；释放请求事务，避免 SSE 长时间占用连接池。
    db.rollback()
    poll_events(engine, request, key, last, settings)

    async def stream() -> AsyncIterator[str]:
        """把数据库中的公开事件转为 SSE 帧，直到读完终态事件或客户端断开。"""
        nonlocal last
        text_received = False
        started = asyncio.get_running_loop().time()
        while not await request.is_disconnected():
            try:
                # 同步数据库查询移到线程中；每次轮询独立开关 Session，不长期占用连接。
                entries, done = await asyncio.to_thread(
                    poll_events, engine, request, key, last, settings
                )
            except HTTPException:
                # 权限撤销或会话过期后关闭流，不继续发送任何任务内容。
                return
            for item in entries:
                # SSE 的 id 用于客户端续传，data 则携带类型、序号和公开业务内容。
                last = int(str(item["sequence"]))
                if selected is not None and item["type"] not in selected:
                    continue
                text_received = text_received or item["type"] == "text_delta"
                encoded = json.dumps(item, ensure_ascii=False)
                yield f"id: {key}:{last}\nevent: {item['type']}\ndata: {encoded}\n\n"
            if entries and selected is not None and entries[-1]["type"] not in selected:
                # 一批被过滤的事件只补一帧游标，既减少传输，也保证断线不会重扫旧事件。
                yield f"id: {key}:{last}\nevent: cursor\ndata: " + json.dumps({
                    "sequence": last, "type": "cursor", "payload": {},
                }) + "\n\n"
            if done:
                return
            yield ": heartbeat\n\n"
            # 首内容前短暂缩短等待，随后恢复普通轮询，避免长任务持续增加数据库负担。
            waiting_first = not text_received and asyncio.get_running_loop().time() - started < 5
            await asyncio.sleep(0.1 if waiting_first else 0.3)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{run_id}/retry", status_code=202)
def retry(
    run_id: UUID,
    request: Request,
    db: DB,
    actor: Writer,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> dict[str, object]:
    """验证权限与当前状态后重新提交处理意图，保留幂等和版本边界。"""
    previous = owned_run(db, str(run_id), actor)
    # 与创建和结算使用相同的父行锁序，串行校验预算与并发额度。
    db.scalar(select(User).where(User.id == previous.account_id).with_for_update())
    db.scalar(select(User).where(User.id == actor.user.id).with_for_update())
    run = db.scalar(
        select(Run)
        .where(Run.id == previous.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert run
    entry = db.scalar(
        select(RunRetry).where(RunRetry.owner_id == actor.user.id, RunRetry.key == idempotency_key)
    )
    if entry:
        if entry.run_id != run.id:
            raise HTTPException(409)
        return run_data(run)
    settings = request.app.state.settings
    from app.skills.service import check_resume_skill

    check_resume_skill(db, run.snapshot, run.account_id)
    check_resume_tools(db, run.account_id, run.owner_id, run.snapshot, settings)
    if (
        run.status != "failed"
        or run.error_code not in RETRYABLE
        or run.attempts >= settings.run_max_attempts
    ):
        raise HTTPException(409)
    try:
        model_settings(settings, run.snapshot)
    except ProviderFailure as error:
        raise HTTPException(409, detail={"code": str(error)}) from error
    active = select(Run).where(Run.status.in_(ACTIVE))
    if db.scalar(active.where(Run.conversation_id == run.conversation_id).limit(1)):
        raise HTTPException(409)
    for column, value, cap in (
        (Run.account_id, run.account_id, settings.workspace_concurrency),
        (Run.owner_id, run.owner_id, settings.user_concurrency),
    ):
        if (
            db.scalar(select(func.count()).select_from(active.where(column == value).subquery()))
            or 0
        ) >= cap:
            raise HTTPException(429, detail={"code": "run_concurrency_exceeded"})
    for budget_key, cap in (
        ("space:" + run.account_id, settings.workspace_token_budget),
        ("user:" + run.owner_id, settings.user_token_budget),
    ):
        budget = db.scalar(select(Budget).where(Budget.key == budget_key).with_for_update())
        assert budget
        if (
            settings.cumulative_budget_enabled
            and budget.spent + budget.reserved + run.reserved_tokens > cap
        ):
            raise HTTPException(429, detail={"code": "run_budget_insufficient"})
        budget.reserved += run.reserved_tokens
    run.status, run.error_code, run.finished_at = "queued", None, None
    run.cancel_requested, run.last_dispatched_at = False, None
    # 重试仅恢复可重试错误，并保留已经消耗的执行时钟。
    run.state_version += 1
    db.add(RunRetry(run_id=run.id, owner_id=actor.user.id, key=idempotency_key))
    audit(db, actor.user, "run.retry", run.account_id, run.id)
    append_event(
        db,
        run,
        "tool_status",
        {"stage": "recovery", "summary": "用户确认从检查点恢复，模型可能再次计费"},
    )
    db.commit()
    try:
        request.app.state.dispatch_run(run.id)
    except Exception:
        pass
    return run_data(run)

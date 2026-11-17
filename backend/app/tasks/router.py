"""任务提交、取消、检查点重试及公开事件流。"""

import asyncio
import hashlib
import json
import random
from collections.abc import AsyncIterator
from datetime import UTC, timedelta
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from app.accounts.access import AccountId, membership
from app.agents.contracts import effective_config
from app.agents.model_config import model_settings
from app.agents.models import Agent, AgentVersion
from app.agents.routing import simple_question
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
from app.tasks.contracts import RunInput, SteeringInput
from app.tasks.execution import append_event
from app.tasks.ledger_models import RunRetry
from app.tasks.models import Budget, Run, RunEvent
from app.tasks.reliability import retryable_run, stop_slice, utc
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
    fingerprint_data = data.model_dump(mode="json")
    # 无附件请求保留旧哈希格式，让更新前的重试仍能命中原任务。
    if not data.uploaded_files:
        fingerprint_data.pop("uploaded_files")
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_data, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    # 幂等键识别同一次提交，内容哈希确认重发没有换参数；命中后不再保存消息或预留预算。
    previous = db.scalar(
        select(Run).where(Run.owner_id == actor.user.id, Run.idempotency_key == idempotency_key)
    )
    if previous:
        matches = previous.request_hash == fingerprint
        if not matches and not data.uploaded_files and data.research_project_id is None:
            # 研究与附件字段引入前的任务没有 research_project_id:null。
            fingerprint_data.pop("research_project_id")
            matches = previous.request_hash == hashlib.sha256(
                json.dumps(fingerprint_data, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
        if not matches:
            raise HTTPException(409)
        return run_data(previous)
    research = None
    if data.research_project_id:
        from app.research.router import owned_project

        research = owned_project(db, str(data.research_project_id), actor.user.id, lock=True)
        if research.state.get("run_id") or data.knowledge_base_id or data.skill_version_id:
            raise HTTPException(409)
        if data.message != research.question:
            raise HTTPException(422)
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
    if data.uploaded_files:
        if sum(len(file.text.encode("utf-8")) for file in data.uploaded_files) > 90000:
            raise HTTPException(413)
        config["uploaded_files"] = [file.model_dump() for file in data.uploaded_files]
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
    if research:
        mock_search = config["provider"] == "mock" and "web_search" in cast(
            list[str], version.config["tools"]
        )
        if (research.options["web_enabled"] and "web_search" not in config["tools"]
                and not mock_search):
            raise HTTPException(409, detail={"code": "research_search_not_authorized"})
        config["research_project_id"] = research.id
        config["timeout_seconds"] = research.options["timeout_seconds"]
    # 自动知识库任务使用执行助手；显式指定知识库的旧客户端保留原路径。
    has_mcp = any(item["kind"] == "mcp" for item in config["tool_catalog"].values())
    config["router_mode"] = has_mcp or not bool(data.knowledge_base_id)
    from app.rag.pipeline import local_plan

    # 闭环只为无历史的明确问答走快速路径，避免把复杂任务的补充要求误判为新问题。
    simple_closed_loop = bool(
        config.get("closed_loop") and not research and simple_question(data.message)
        and db.scalar(select(Message.id).where(Message.conversation_id == key).limit(1)) is None
    )
    config["direct_mode"] = bool(
        config["router_mode"] and (not config.get("closed_loop") or simple_closed_loop)
        and not config.get("skill")
        and not has_mcp
        and local_plan(data.message).complexity == "simple"
    )
    if config["router_mode"]:
        from app.rag.contracts import freeze_rag_config
        from app.rag.embeddings import Embeddings

        config["rag_config"] = freeze_rag_config(settings)
        config["rag_embedding_version"] = Embeddings(settings).version
    if data.knowledge_base_id:
        from app.rag.models import KnowledgeBase

        base = db.get(KnowledgeBase, str(data.knowledge_base_id))
        if base is None or base.deleted or base.account_id != conversation.account_id:
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
    # 同一会话允许多个任务，空间和用户的并发上限继续生效。
    active = select(Run).where(Run.status.in_(ACTIVE))
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
    db.add(Message(
        conversation_id=key, sequence=sequence, role="user", content=data.message,
        uploaded_files=[file.model_dump() for file in data.uploaded_files],
    ))
    config["conversation_upto_sequence"] = sequence
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
    if research:
        research.state = {"run_id": run.id, "phase": "researching"}
    from app.tasks.retention import prune_user_history

    prune_user_history(db, actor.user.id)
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
    """返回最近 100 条任务及全部活动任务，旧暂停任务仍可继续或停止。"""
    key = str(conversation_id)
    owned_conversation(db, key, actor)
    recent = select(Run.id).where(Run.conversation_id == key).order_by(
        Run.created_at.desc(), Run.id.desc()
    ).limit(100)
    rows = db.scalars(select(Run).where(
        Run.conversation_id == key, (Run.id.in_(recent) | Run.status.in_(ACTIVE)),
    ).order_by(Run.created_at.desc(), Run.id.desc()))
    return {"items": [run_data(row) for row in rows]}


@router.get("/me/runs")
def workspace_runs(
    account_id: AccountId, db: DB, actor: Actor, limit: Limit = 20, cursor: UUID | None = None
) -> dict[str, object]:
    """按时间倒序分页返回本人最近 100 条及全部活动任务。"""
    key = str(account_id)
    membership(db, key, actor.user.id)
    from app.tasks.retention import HISTORY_LIMIT

    recent = select(Run.id).where(Run.owner_id == actor.user.id).order_by(
        Run.created_at.desc(), Run.id.desc()
    ).limit(HISTORY_LIMIT)
    query = select(Run, Conversation.title).join(
        Conversation, Conversation.id == Run.conversation_id
    )
    query = query.where(
        Run.account_id == key, Run.owner_id == actor.user.id,
        Run.id.in_(recent) | Run.status.in_(ACTIVE),
    )
    if cursor:
        boundary = owned_run(db, str(cursor), actor)
        query = query.where((Run.created_at < boundary.created_at) | (
            (Run.created_at == boundary.created_at) & (Run.id < boundary.id)
        ))
    rows = db.execute(query.order_by(Run.created_at.desc(), Run.id.desc()).limit(limit + 1))
    return page([{**run_data(row), "conversation_title": title,
                  "prompt_preview": " ".join(row.prompt.split())[:160],
                  "created_at": row.created_at.isoformat()} for row, title in rows], limit)


@router.post("/runs/{run_id}/steer", status_code=202)
def steer_run(
    run_id: UUID,
    data: SteeringInput,
    db: DB,
    actor: Writer,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> dict[str, object]:
    """持久化原任务的插话；与结算采用相同锁序，重试不重复保存消息。"""
    previous = owned_run(db, str(run_id), actor)
    db.scalar(select(User).where(User.id == previous.account_id).with_for_update())
    db.scalar(select(User).where(User.id == actor.user.id).with_for_update())
    db.scalar(
        select(Conversation).where(Conversation.id == previous.conversation_id).with_for_update()
    )
    run = db.scalar(
        select(Run).where(Run.id == previous.id).with_for_update()
        .execution_options(populate_existing=True)
    )
    assert run is not None
    fingerprint = hashlib.sha256(data.message.encode()).hexdigest()
    instructions = list(cast(list[dict[str, object]], run.snapshot.get("steering", [])))
    for instruction in instructions:
        if instruction["key"] == idempotency_key:
            if instruction["hash"] != fingerprint:
                raise HTTPException(409, detail={"code": "steering_conflict"})
            return run_data(run)
    if not data.message.strip():
        raise HTTPException(422)
    if run.status not in {"queued", "running", "paused"} or run.cancel_requested:
        raise HTTPException(409, detail={"code": "steering_unavailable"})
    if run.snapshot.get("research_project_id"):
        raise HTTPException(409, detail={"code": "steering_unsupported"})
    if len(instructions) >= 20:
        raise HTTPException(429, detail={"code": "steering_limit"})
    instructions.append({
        "key": idempotency_key, "hash": fingerprint, "message": data.message.strip(),
    })
    run.snapshot = {**run.snapshot, "steering": instructions}
    run.state_version += 1
    sequence = (db.scalar(select(func.max(Message.sequence)).where(
        Message.conversation_id == run.conversation_id,
    )) or 0) + 1
    db.add(Message(
        conversation_id=run.conversation_id, sequence=sequence,
        role="user", content=data.message.strip(),
    ))
    append_event(db, run, "tool_status", {"stage": "steering_queued"})
    audit(db, actor.user, "run.steer", run.account_id, run.id)
    db.commit()
    return run_data(run)


@router.post("/runs/{run_id}/pause")
def pause_run(run_id: UUID, db: DB, actor: Writer) -> dict[str, object]:
    """保留执行栈与租约，暂停排队或运行中的任务；重复请求不改变状态。"""
    key = str(run_id)
    owned_run(db, key, actor)
    run = db.scalar(
        select(Run).where(Run.id == key).with_for_update().execution_options(populate_existing=True)
    )
    assert run is not None
    if run.status in {"queued", "running"}:
        if run.slice_started_at:
            run.active_seconds += max(0, int((now() - utc(run.slice_started_at)).total_seconds()))
        run.slice_started_at = None
        run.status = "paused"
        run.state_version += 1
        append_event(db, run, "run_state", {"status": "paused"})
        audit(db, actor.user, "run.pause", run.account_id, run.id)
        db.commit()
    return run_data(run)


@router.post("/runs/{run_id}/resume")
def resume_run(run_id: UUID, db: DB, actor: Writer, request: Request) -> dict[str, object]:
    """优先唤醒持有租约的原执行栈；尚未领取或失联的任务沿用检查点排队。"""
    key = str(run_id)
    owned_run(db, key, actor)
    run = db.scalar(
        select(Run).where(Run.id == key).with_for_update().execution_options(populate_existing=True)
    )
    assert run is not None
    if run.status != "paused":
        return run_data(run)
    if run.lease_token and run.lease_until and utc(run.lease_until) > now():
        run.status, run.slice_started_at = "running", now()
    else:
        stop_slice(run)
        run.status, run.last_dispatched_at = "queued", None
    run.state_version += 1
    append_event(db, run, "run_state", {"status": run.status})
    audit(db, actor.user, "run.resume", run.account_id, run.id)
    db.commit()
    if run.status == "queued":
        try:
            request.app.state.dispatch_run(key)
        except Exception:
            # 排队意图已提交，投递失败由恢复调度重试。
            pass
    return run_data(run)


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
        "run_state",
    }
    if selected is not None:
        if not selected <= allowed:
            raise HTTPException(422)
        # 终态和恢复事件必须送达，订阅者不能过滤掉决定结束或替换正文的控制事件。
        selected |= {"completed", "error", "clarification", "checkpoint_restored", "run_state"}
    engine: Engine = request.app.state.database
    # 在发出响应头前校验过期游标；释放请求事务，避免 SSE 长时间占用连接池。
    db.rollback()
    poll_events(engine, request, key, last, settings)

    async def stream() -> AsyncIterator[str]:
        """把数据库中的公开事件转为 SSE 帧，直到读完终态事件或客户端断开。"""
        nonlocal last
        admission = request.app.state.database_admission
        with request.app.state.event_hub.subscribe(key) as changed:
            while not await request.is_disconnected():
                # 先清标记再读库，读库期间的提交仍会唤醒下一轮；不信任通知正文。
                changed.clear()
                try:
                    entries, done = await admission.run(
                        poll_events, engine, request, key, last, settings
                    )
                except HTTPException:
                    # 权限失效或准入过载时关闭流，客户端可从原游标重连。
                    return
                for item in entries:
                    # SSE 的 id 用于客户端续传，data 携带公开业务内容。
                    last = int(str(item["sequence"]))
                    if selected is not None and item["type"] not in selected:
                        continue
                    encoded = json.dumps(item, ensure_ascii=False)
                    yield f"id: {key}:{last}\nevent: {item['type']}\ndata: {encoded}\n\n"
                if entries and selected is not None and entries[-1]["type"] not in selected:
                    # 过滤掉的事件也推进游标，避免断线后重复扫描。
                    yield f"id: {key}:{last}\nevent: cursor\ndata: " + json.dumps({
                        "sequence": last, "type": "cursor", "payload": {},
                    }) + "\n\n"
                if done:
                    return
                if len(entries) == 100:
                    continue
                yield ": heartbeat\n\n"
                try:
                    # 定期补读兼顾漏通知、Redis 故障及闲置流的权限撤销；抖动避免同步查库。
                    await asyncio.wait_for(
                        changed.wait(), settings.run_event_fallback_seconds * random.uniform(0.8, 1)
                    )
                except TimeoutError:
                    pass

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
        not retryable_run(run)
        or run.attempts >= settings.run_max_attempts
    ):
        raise HTTPException(409)
    try:
        model_settings(settings, run.snapshot)
    except ProviderFailure as error:
        raise HTTPException(409, detail={"code": str(error)}) from error
    active = select(Run).where(Run.status.in_(ACTIVE))
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

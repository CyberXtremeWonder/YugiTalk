"""租约与恢复协调：PostgreSQL 排队状态就是持久投递意图，Redis 仅负责唤醒。"""

import hashlib
import json
import logging
import threading
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import Engine, and_, or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.entities import now
from app.identity.models import User
from app.providers.client import ProviderFailure, ToolCall
from app.rag.models import Document
from app.tasks.ledger_models import ToolExecution
from app.tasks.models import Run
from app.tools.registry import TOOL_SCHEMAS
from app.tools.runner import execute_tool

RETRYABLE = {
    "model_transient",
    "model_timeout_or_network",
    "queue_unavailable",
    "model_timeout",
    "model_unavailable",
    "model_rate_limited",
    "model_network_error",
    "model_stream_error",
    "model_incomplete",
    "execution_error",
    "worker_lost",
}


def utc(value: datetime) -> datetime:
    """将数据库时间规范到统一时区，兼容测试数据库的无时区结果。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def fingerprint(tool: str, arguments: dict[str, Any]) -> str:
    """规范化后绑定工具名与参数，避免恢复时错用参数已经变化的执行结果。"""
    return hashlib.sha256(
        json.dumps(
            {"tool": tool, "arguments": arguments},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def stop_slice(run: Run) -> None:
    """累计当前执行片段耗时，并清除片段起点及任务租约。"""
    if run.slice_started_at:
        run.active_seconds += max(0, int((now() - utc(run.slice_started_at)).total_seconds()))
    run.slice_started_at = None
    run.lease_token, run.lease_until = None, None
    run.state_version += 1


class LeaseKeeper(AbstractContextManager["LeaseKeeper"]):
    """模型与嵌入 HTTP 阻塞期间续租；过期令牌绝不复活。"""

    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        model: type[Run] | type[Document],
        key: str,
        token: str,
    ) -> None:
        """保存实例所需的依赖与配置，初始化后续执行使用的状态。"""
        self.engine, self.settings, self.model, self.key, self.token = (
            engine,
            settings,
            model,
            key,
            token,
        )
        self.stopped = threading.Event()
        self.thread: threading.Thread | None = None

    def __enter__(self) -> "LeaseKeeper":
        # 单元测试使用单连接 SQLite；真实并发租约由隔离 PostgreSQL 故障测试验证。
        """启动租约维护线程，并返回当前上下文管理实例。"""
        if self.engine.dialect.name == "postgresql":
            self.thread = threading.Thread(target=self.renew, daemon=True)
            self.thread.start()
        return self

    def renew(self) -> None:
        """定期验证并续期租约，在失去所有权时停止维护。"""
        while not self.stopped.wait(self.settings.run_lease_seconds / 3):
            # 续租周期短于租约寿命；只有当前令牌且尚未过期才可延长，旧 Worker 不能复活。
            try:
                with Session(self.engine) as db:
                    row = cast(
                        Run | Document | None,
                        db.scalar(
                            select(self.model).where(self.model.id == self.key).with_for_update()
                        ),
                    )
                    if (
                        not row
                        or row.lease_token != self.token
                        or not row.lease_until
                        or utc(row.lease_until) <= now()
                    ):
                        return
                    row.lease_until = now() + timedelta(seconds=self.settings.run_lease_seconds)
                    db.commit()
            except Exception:
                logging.getLogger(__name__).warning("续租暂不可用，旧租约到期后禁止继续写入")

    def __exit__(self, *args: object) -> None:
        """停止后台租约维护并释放线程相关资源。"""
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=2)


def prepare_bound_tool(
    db: Session, run: Run, call: ToolCall, step: int, settings: Settings | None = None
) -> tuple[ToolExecution, dict[str, Any]]:
    """在租约事务中检查许可、版本和参数指纹，再创建或读取幂等执行账本。"""
    # 即便旧检查点或执行账本仍含下线工具，也不得执行或返回其历史写入结果。
    pins = cast(dict[str, Any], run.snapshot.get("tool_catalog", {}))
    if call.name not in TOOL_SCHEMAS and call.name not in pins:
        raise ProviderFailure("tool_removed")
    from app.guardrails.tools import authorize_tool

    # 即使图层被绕过或账本已成功，也不能通过缓存结果绕过当前任务许可。
    authorize_tool(run.snapshot, call.name)
    from app.tools.service import bound_tool

    pin = bound_tool(db, run.account_id, run.owner_id, run.snapshot, call.name, settings)
    arguments = call.arguments
    digest = fingerprint(call.name, arguments)
    call_key = f"{step}:{call.id}"
    # 同一任务中的模型步数与调用 ID 确定账本条目，参数哈希另行防止同 ID 换参数。
    item = db.scalar(
        select(ToolExecution).where(
            ToolExecution.run_id == run.id, ToolExecution.call_key == call_key
        )
    )
    if item is None:
        item = ToolExecution(
            run_id=run.id,
            call_key=call_key,
            tool=call.name,
            arguments=arguments,
            argument_hash=digest,
        )
        db.add(item)
        db.flush()
    if (
        item.tool != call.name
        or item.argument_hash != digest
        or fingerprint(item.tool, item.arguments) != digest
    ):
        raise ProviderFailure("tool_arguments_changed")
    return item, pin


def execute_bound_tool(
    db: Session, run: Run, call: ToolCall, step: int, settings: Settings | None = None
) -> dict[str, object]:
    """内置工具在短事务内执行；网络查询由执行器分离事务，避免阻塞取消和续租。"""
    item, pin = prepare_bound_tool(db, run, call, step, settings)
    # 工具结果已经提交，恢复时直接复用，不再执行。
    if item.status == "succeeded":
        return item.result
    if pin["kind"] != "builtin":
        raise ProviderFailure("tool_not_authorized")
    result = execute_tool(call.name, call.arguments)
    item.status, item.result = "succeeded", result
    return result


def sweep(
    engine: Engine, settings: Settings, dispatch: Any, dispatch_document: Any
) -> dict[str, int]:
    """有界扫描并按行锁协调多个调度器；重复投递由领取和执行账本吸收。"""
    from app.tasks.execution import append_event, finish_run

    stats = {"runs": 0, "documents": 0, "expired": 0}
    cutoff = now() - timedelta(seconds=max(5, settings.recovery_interval_seconds * 2))
    due = now()
    absent_member = Run.account_id != Run.owner_id
    inactive_user = ~select(User.id).where(User.id == Run.owner_id, User.active.is_(True)).exists()
    with Session(engine) as db:
        keys = list(
            db.scalars(
                select(Run.id)
                .where(Run.status.in_(["queued", "running", "waiting_approval", "waiting_input"]))
                .where(
                    or_(
                        and_(
                            Run.status == "queued",
                            or_(Run.last_dispatched_at.is_(None), Run.last_dispatched_at <= cutoff),
                        ),
                        and_(
                            Run.status == "running",
                            or_(Run.lease_until.is_(None), Run.lease_until <= due),
                        ),
                        Run.status == "waiting_approval",
                        and_(
                            Run.status == "waiting_input",
                            Run.snapshot["rag_clarification"]["expires_at"].as_string()
                            <= due.isoformat(),
                        ),
                        absent_member,
                        inactive_user,
                    )
                )
                .order_by(Run.created_at)
                .limit(200)
            )
        )
    for key in keys:
        failure = None
        # 先筛选候选 ID，再逐条短事务加锁；多个调度器跳过对方正在处理的任务。
        with Session(engine) as db:
            run = db.scalar(select(Run).where(Run.id == key).with_for_update(skip_locked=True))
            if not run or run.status not in {
                "queued",
                "running",
                "waiting_approval",
                "waiting_input",
            }:
                continue
            user = db.get(User, run.owner_id)
            if not user or not user.active or run.account_id != run.owner_id:
                failure = "permission_revoked"
            elif run.status == "waiting_approval":
                # 兼容下线前已暂停的任务，统一结束并释放预算，避免永久占用活动额度。
                failure = "tool_removed"
            elif run.status == "waiting_input":
                failure = "rag_clarification_expired"
            elif run.status == "running":
                # 租约到期才允许接管；保留已有检查点，重排队后从已提交的边界恢复。
                if run.lease_until and utc(run.lease_until) > now():
                    continue
                stop_slice(run)
                if run.attempts >= settings.run_max_attempts:
                    failure = "worker_lost"
                else:
                    run.status = "queued"
                    append_event(
                        db,
                        run,
                        "tool_status",
                        {"stage": "recovery", "summary": "租约到期，从检查点恢复"},
                    )
            if failure:
                # 先使旧 Worker 失去令牌，随后用统一锁序进行终态与预算结算。
                stop_slice(run)
                db.commit()
            elif run.status == "queued" and (
                not run.last_dispatched_at or utc(run.last_dispatched_at) <= cutoff
            ):
                try:
                    dispatch(key)
                    run.last_dispatched_at = now()
                    stats["runs"] += 1
                except Exception:
                    logging.getLogger(__name__).warning("任务投递暂不可用，保留数据库排队意图")
                db.commit()
            else:
                db.commit()
        if failure:
            finish_run(engine, key, "failed", failure, "", 0, None, None)
            stats["expired"] += 1
    with Session(engine) as db:
        keys = list(
            db.scalars(
                select(Document.id)
                .where(Document.status.in_(["queued", "parsing", "indexing", "deleting"]))
                .where(
                    or_(
                        and_(
                            Document.status.in_(["queued", "deleting"]),
                            or_(
                                Document.last_dispatched_at.is_(None),
                                Document.last_dispatched_at <= cutoff,
                            ),
                        ),
                        and_(
                            Document.status.in_(["parsing", "indexing"]),
                            or_(Document.lease_until.is_(None), Document.lease_until <= due),
                        ),
                    )
                )
                .order_by(Document.created_at)
                .limit(200)
            )
        )
    for key in keys:
        with Session(engine) as db:
            doc = db.scalar(
                # 文档恢复单独处理，其代数递增可隔离跨存储遗留的旧向量写入。
                select(Document).where(Document.id == key).with_for_update(skip_locked=True)
            )
            if not doc or doc.status not in {"queued", "parsing", "indexing", "deleting"}:
                continue
            if doc.status in {"parsing", "indexing"}:
                if doc.lease_until and utc(doc.lease_until) > now():
                    continue
                if doc.attempts >= settings.run_max_attempts:
                    doc.status, doc.error_code = "failed", "document_worker_lost"
                    doc.progress = {
                        **doc.progress,
                        "failed_stage": doc.progress.get("stage", "queued"),
                    }
                    db.commit()
                    continue
                # 新版本隔离旧 Worker 写入，原文重读后按确定性分块 ID 幂等重建。
                doc.index_version += 1
                doc.status, doc.progress = "queued", {"stage": "queued", "recovered": True}
                doc.lease_token, doc.lease_until = None, None
            if not doc.last_dispatched_at or utc(doc.last_dispatched_at) <= cutoff:
                try:
                    dispatch_document(key, doc.index_version, doc.status == "deleting")
                    doc.last_dispatched_at = now()
                    stats["documents"] += 1
                except Exception:
                    logging.getLogger(__name__).warning("资料投递暂不可用，保留数据库状态")
            db.commit()
    return stats

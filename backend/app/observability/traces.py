"""统一步骤记录入口，通过上下文传递任务和父步骤，不依赖采样。"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.core.entities import identifier, now
from app.observability.trace_models import TraceStep

_recorder: ContextVar[tuple[Engine, str, int] | None] = ContextVar("trace_recorder", default=None)
_parent: ContextVar[str | None] = ContextVar("trace_parent", default=None)
_iteration: ContextVar[int | None] = ContextVar("trace_iteration", default=None)
_current: ContextVar["Step | None"] = ContextVar("trace_step", default=None)


@dataclass
class Step:
    """只接受调用方明确传入的公开统计数据。"""

    status: str = "succeeded"
    input_tokens: int | None = None
    output_tokens: int | None = None
    details: dict[str, object] = field(default_factory=dict)

    def usage(self, reply: Any) -> None:
        """供应商缺失用量时保留未知，缓存输入沿用协议解析口径。"""
        self.input_tokens = reply.input_tokens
        self.output_tokens = reply.output_tokens


def trace_details(**details: object) -> None:
    """为当前步骤补充命中数量等元数据。"""
    step = _current.get()
    if step:
        step.details.update(details)


@contextmanager
def recording(engine: Engine, run_id: str, attempt: int) -> Iterator[None]:
    """每次 Worker 执行独立绑定，异步 RAG 子任务继承同一上下文。"""
    token = _recorder.set((engine, run_id, attempt))
    try:
        yield
    finally:
        _recorder.reset(token)


@contextmanager
def trace_step(
    kind: str, name: str, iteration: int | None = None, **details: object
) -> Iterator[Step]:
    """持久化步骤开始和结束，异常只记录类型及已定义错误码。"""
    step = Step(details=details)
    recorder = _recorder.get()
    if recorder is None:
        yield step
        return
    engine, run_id, attempt = recorder
    key = identifier()
    started = time.monotonic()
    with Session(engine) as db:
        parent_id = _parent.get()
        if kind == "tool" and iteration is not None:
            # 工具节点运行时模型节点已退出，将工具挂回同一轮 loop，形成完整的模型/工具树。
            loop = db.scalar(
                select(TraceStep)
                .where(
                    TraceStep.run_id == run_id,
                    TraceStep.attempt == attempt,
                    TraceStep.kind == "loop",
                    TraceStep.iteration == iteration,
                )
                .order_by(TraceStep.started_at.desc())
                .limit(1)
            )
            if loop:
                parent_id = loop.id
                loop.status = "running"
                loop.finished_at = None
                loop.duration_ms = None
        db.add(
            TraceStep(
                id=key,
                run_id=run_id,
                parent_id=parent_id,
                attempt=attempt,
                iteration=iteration if iteration is not None else _iteration.get(),
                kind=kind,
                name=name,
                details=details,
            )
        )
        db.commit()
    parent_token = _parent.set(key)
    # ContextVar 隔离并发执行的父步骤；退出后恢复旧值，让兄弟步骤不会误成父子关系。
    iteration_token = _iteration.set(iteration if iteration is not None else _iteration.get())
    current_token = _current.set(step)
    try:
        yield step
    except BaseException as error:
        step.status = "failed" if isinstance(error, Exception) else "interrupted"
        step.details["error_type"] = type(error).__name__
        # 不收集异常消息或任意对象内容。
        from app.providers.client import ProviderFailure

        if isinstance(error, ProviderFailure):
            step.details["error_code"] = error.code
        raise
    finally:
        _current.reset(current_token)
        _iteration.reset(iteration_token)
        _parent.reset(parent_token)
        with Session(engine) as db:
            row = db.get(TraceStep, key)
            if row:
                row.status, row.finished_at = step.status, now()
                row.duration_ms = round((time.monotonic() - started) * 1000)
                row.input_tokens, row.output_tokens = step.input_tokens, step.output_tokens
                row.details = step.details
                if kind == "tool" and parent_id:
                    loop = db.get(TraceStep, parent_id)
                    if loop and loop.kind == "loop":
                        loop.finished_at = row.finished_at
                        start = loop.started_at
                        if start.tzinfo is None:
                            start = start.replace(tzinfo=UTC)
                        loop.duration_ms = round((row.finished_at - start).total_seconds() * 1000)
                        loop.status = step.status
                db.commit()


def usage_summary(rows: list[TraceStep]) -> dict[str, int]:
    """累计所有真实模型请求，失败及缺用量请求计入未知数量。"""
    calls = [row for row in rows if row.kind == "llm"]
    # 只累计实际模型请求节点，避免把外层 loop 的用量重复计算；缺失用量另计未知次数。
    return {
        "model_calls": len(calls),
        "input_tokens": sum(row.input_tokens or 0 for row in calls),
        "output_tokens": sum(row.output_tokens or 0 for row in calls),
        "unknown_usage_calls": sum(
            row.input_tokens is None or row.output_tokens is None for row in calls
        ),
        "unrecorded_attempts": max(0, min((row.attempt for row in rows), default=1) - 1),
    }


def run_usage(db: Session, run_id: str) -> dict[str, int]:
    """终态结算读取已完成的 Agent 和 RAG 模型请求。"""
    return usage_summary(list(db.scalars(select(TraceStep).where(TraceStep.run_id == run_id))))

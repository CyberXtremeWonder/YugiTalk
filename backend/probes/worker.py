"""在 Linux 容器中验证 Celery、Redis 与 PostgreSQL 检查点协作。"""

import os
from typing import Any

from celery import Celery
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command

from probes.graph import build_graph

queue = Celery(
    "stage_zero",
    broker=os.getenv("PROBE_REDIS_URL", "redis://localhost:16379/0"),
    backend=os.getenv("PROBE_REDIS_URL", "redis://localhost:16379/0"),
)
queue.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    result_expires=600,
)


def database_url() -> str:
    """返回隔离验证库的连接地址，禁止连接正式业务库。"""
    return os.getenv(
        "PROBE_DATABASE_URL",
        "postgresql://probe:probe-local-only@localhost:15432/probe",
    )


@queue.task(name="stage_zero.advance")  # type: ignore[untyped-decorator]
def advance(thread_id: str, decision: str | None = None) -> dict[str, Any]:
    """每次任务创建新的存储器，证明恢复依赖持久化而非内存对象。"""
    with PostgresSaver.from_conn_string(database_url()) as saver:
        graph = build_graph(saver)
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        if decision is None:
            graph.invoke({"prompt": "创建演示工单"}, config)
        else:
            graph.invoke(Command(resume=decision), config)
        snapshot = graph.get_state(config)
        return {"values": snapshot.values, "waiting": bool(snapshot.next)}


if __name__ == "__main__":
    # 检查点建表通过独立命令运行，避免每个 Worker 任务竞争执行初始化。
    with PostgresSaver.from_conn_string(database_url()) as checkpoint:
        checkpoint.setup()

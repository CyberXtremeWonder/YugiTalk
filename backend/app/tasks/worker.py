"""基础设施探测与 Agent 执行任务；业务结果持久化在 PostgreSQL。"""

from opentelemetry import propagate

from app.observability.telemetry import worker_telemetry
from app.tasks.celery_app import celery_app, settings
from app.tasks.database import database
from app.tasks.execution import Execution


# Celery 装饰器没有完整类型标记，只豁免该装饰器，任务本身保持类型约束。
@celery_app.task(name="yugitalk.infrastructure.ping")  # type: ignore[untyped-decorator]
def ping() -> dict[str, str]:
    """真实队列验证使用独立诊断任务，没有外部副作用。"""
    return {"status": "ok"}


@celery_app.task(name="yugitalk.agent.execute")  # type: ignore[untyped-decorator]
def execute_agent(run_id: str) -> None:
    """只传递运行 ID，敏感提示词不进入队列参数或结果后端。"""
    engine, settings = database.get()
    # 接续 API 投递时的追踪上下文，便于把提交请求与后台执行串起来查看。
    headers = getattr(celery_app.current_task.request, "headers", None) or {}
    with worker_telemetry().span("agent.execute", propagate.extract(headers)) as span:
        span.set_attribute("run.id", run_id)
        # 队列只负责唤醒；Execution 会从数据库领取任务，并阻止重复投递造成并发执行。
        Execution(engine, settings, run_id).execute()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="yugitalk.knowledge.index",
    soft_time_limit=settings.document_index_timeout_seconds,
    time_limit=settings.document_index_timeout_seconds + 10,
)
def import_document(document_id: str, generation: int) -> None:
    """按文档索引版本幂等导入，队列参数不包含原文。"""
    from app.rag.indexing import index_document

    engine, settings = database.get()
    headers = getattr(celery_app.current_task.request, "headers", None) or {}
    with worker_telemetry().span("knowledge.index", propagate.extract(headers)):
        index_document(engine, settings, document_id, generation)


@celery_app.task(name="yugitalk.knowledge.cleanup")  # type: ignore[untyped-decorator]
def cleanup_knowledge(document_id: str) -> None:
    """墓碑提交后再异步清除向量和原文，失败可重复执行。"""
    from app.rag.indexing import cleanup_document

    engine, settings = database.get()
    headers = getattr(celery_app.current_task.request, "headers", None) or {}
    with worker_telemetry().span("knowledge.cleanup", propagate.extract(headers)):
        cleanup_document(engine, settings, document_id)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="yugitalk.knowledge.graph", soft_time_limit=185, time_limit=195,
)
def import_graph(document_id: str, generation: int) -> None:
    """独立构建图谱，沿用后台模型配置并保持文档问答可用。"""
    from app.rag.graph import build_graph

    engine, settings = database.get()
    with worker_telemetry().span("knowledge.graph"):
        build_graph(engine, settings, document_id, generation)

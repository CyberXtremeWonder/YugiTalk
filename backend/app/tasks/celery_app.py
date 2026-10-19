"""队列只接受 JSON，避免反序列化任意 Python 对象。"""

from celery import Celery
from celery.signals import setup_logging

from app.core.config import load_settings
from app.core.logging import configure_logging

settings = load_settings()


@setup_logging.connect  # type: ignore[untyped-decorator]
def setup_worker_logging(**kwargs: object) -> None:
    """让后台任务沿用结构化日志格式，保留关联标识与诊断字段。"""
    configure_logging(settings.log_level)


celery_app = Celery(
    "yugitalk",
    broker=settings.redis_url.get_secret_value(),
    backend=settings.redis_url.get_secret_value(),
    include=["app.tasks.worker"],
)
celery_app.conf.update(
    task_default_queue=settings.task_queue,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=settings.task_timeout_seconds,
    task_time_limit=settings.task_timeout_seconds + 10,
    result_expires=600,
    broker_connection_retry_on_startup=True,
    broker_connection_timeout=3,
    broker_transport_options={
        "socket_timeout": 3,
        "socket_connect_timeout": 3,
        "visibility_timeout": settings.run_lease_seconds * 2,
    },
    task_publish_retry=False,
)

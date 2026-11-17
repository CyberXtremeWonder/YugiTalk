"""独立恢复调度器，本机与服务器使用相同入口；停机时只停止投递。"""

import logging
import signal
import threading
import time
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings
from app.core.database import create_database
from app.core.logging import configure_logging, log_failure
from app.identity.retention import cleanup_expired
from app.rag.graph import sweep_graph
from app.rag.indexing import dispatch_document
from app.tasks.execution import dispatch_run
from app.tasks.reliability import sweep
from app.tasks.retention import prune_history


def cleanup_identity_if_due(engine: Engine, settings: Settings, next_due: float) -> float:
    """身份维护独立限频，数据库失败只记录安全元数据，不阻断恢复调度。"""
    if not settings.identity_cleanup_enabled or time.monotonic() < next_due:
        return next_due
    logger = logging.getLogger(__name__)
    try:
        result = cleanup_expired(engine, batch_size=settings.identity_cleanup_batch_size)
        logger.info(
            "过期身份清理完成：sessions=%s codes=%s cutoff=%s",
            result["login_sessions"],
            result["email_verifications"],
            result["cutoff"],
            extra={"operation": "identity.cleanup"},
        )
    except SQLAlchemyError as error:
        log_failure(logger, "identity.cleanup", error)
    return time.monotonic() + settings.identity_cleanup_interval_seconds


def serve(settings: Settings) -> None:
    """周期性执行恢复扫描，补投任务并处理失效租约。"""
    configure_logging(settings.log_level)
    stop = threading.Event()
    for code in (signal.SIGTERM, signal.SIGINT):
        signal.signal(code, lambda *_: stop.set())
    engine = create_database(settings)
    next_identity_cleanup = 0.0
    marker = (
        Path("/tmp/yugitalk-scheduler")
        if Path("/tmp").exists()
        else Path("../.cache/scheduler-live")
    )
    try:
        while not stop.is_set():
            try:
                sweep(engine, settings, dispatch_run, dispatch_document)
                sweep_graph(engine, settings)
                prune_history(engine)
                # 仅成功扫描后更新心跳文件；部署探针可据修改时间判断调度器是否持续工作。
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.touch()
            except Exception:
                logging.getLogger(__name__).warning("恢复调度暂不可用，下个周期重新检查")
            next_identity_cleanup = cleanup_identity_if_due(engine, settings, next_identity_cleanup)
            stop.wait(settings.recovery_interval_seconds)
    finally:
        engine.dispose()

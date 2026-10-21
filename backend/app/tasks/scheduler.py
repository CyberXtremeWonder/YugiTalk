"""独立恢复调度器，本机与服务器使用相同入口；停机时只停止投递。"""

import logging
import signal
import threading
from pathlib import Path

from app.core.config import Settings
from app.core.database import create_database
from app.core.logging import configure_logging
from app.rag.graph import sweep_graph
from app.rag.indexing import dispatch_document
from app.tasks.execution import dispatch_run
from app.tasks.reliability import sweep


def serve(settings: Settings) -> None:
    """周期性执行恢复扫描，补投任务并处理失效租约。"""
    configure_logging(settings.log_level)
    stop = threading.Event()
    for code in (signal.SIGTERM, signal.SIGINT):
        signal.signal(code, lambda *_: stop.set())
    engine = create_database(settings)
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
                # 仅成功扫描后更新心跳文件；部署探针可据修改时间判断调度器是否持续工作。
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.touch()
            except Exception:
                logging.getLogger(__name__).warning("恢复调度暂不可用，下个周期重新检查")
            stop.wait(settings.recovery_interval_seconds)
    finally:
        engine.dispose()

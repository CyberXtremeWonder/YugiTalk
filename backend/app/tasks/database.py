"""Celery子进程独占数据库连接池，任务结束释放会话，进程结束释放池。"""

import os
from typing import Any

from celery.signals import worker_process_shutdown, worker_shutdown
from sqlalchemy import Engine

from app.core.config import Settings, load_settings
from app.core.database import create_database


class ProcessDatabase:
    """为串行执行任务的Worker进程延迟建立池，拒绝复用fork继承连接。"""

    def __init__(self) -> None:
        """只记录资源状态，导入模块和父进程启动时不建立数据库连接。"""
        self.pid: int | None = None
        self.engine: Engine | None = None
        self.settings: Settings | None = None

    def get(self) -> tuple[Engine, Settings]:
        """同一子进程复用引擎；进程身份变化时丢弃继承池但不关闭父进程连接。"""
        pid = os.getpid()
        if self.engine is not None and self.pid != pid:
            # fork 后套接字可能继承自父进程，只替换本进程的池引用，不关闭父进程连接。
            self.engine.dispose(close=False)
            self.engine, self.settings = None, None
        if self.engine is None:
            settings = load_settings()
            engine = create_database(settings)
            self.engine, self.settings, self.pid = engine, settings, pid
        assert self.settings is not None
        return self.engine, self.settings

    def close(self) -> None:
        """正常关闭自身连接；继承资源只替换池，避免影响仍运行的父进程。"""
        if self.engine is not None:
            self.engine.dispose(close=self.pid == os.getpid())
        self.engine, self.settings, self.pid = None, None, None


database = ProcessDatabase()


@worker_process_shutdown.connect  # type: ignore[untyped-decorator]
@worker_shutdown.connect  # type: ignore[untyped-decorator]
def close_worker_database(**kwargs: Any) -> None:
    """接收prefork子进程或solo主进程的退出信号，幂等释放连接池。"""
    database.close()

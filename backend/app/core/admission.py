"""数据库准入在协程中等待，避免排队请求占满同步依赖线程。"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TypeVar

import anyio
from fastapi import HTTPException

from app.core.config import Settings

T = TypeVar("T")


class DatabaseAdmission:
    """每个 API 进程独立限制数据库工作量及排队时间。"""

    def __init__(self, settings: Settings) -> None:
        """只建立内存状态，不连接数据库或创建线程。"""
        self.slots = asyncio.BoundedSemaphore(settings.api_database_concurrency)
        self.queue_limit = settings.api_database_queue_limit
        self.timeout = settings.api_database_queue_timeout_seconds
        self.waiting = 0
        # 清理不竞争鉴权使用的默认线程令牌，取消请求也必须先归还连接。
        self.cleanup = anyio.CapacityLimiter(settings.api_database_concurrency)

    @asynccontextmanager
    async def enter(self) -> AsyncIterator[None]:
        """有界等待一个工作许可，超载时快速返回可重试的 503。"""
        if not self.slots.locked():
            await self.slots.acquire()
        else:
            if self.waiting >= self.queue_limit:
                raise self.overloaded()
            self.waiting += 1
            try:
                try:
                    await asyncio.wait_for(self.slots.acquire(), timeout=self.timeout)
                except TimeoutError as error:
                    raise self.overloaded() from error
            finally:
                self.waiting -= 1
        try:
            yield
        finally:
            self.slots.release()

    async def run(self, operation: Callable[..., T], *args: object) -> T:
        """让 SSE 短查询与普通请求共同遵守数据库并发预算。"""
        async with self.enter():
            return await anyio.to_thread.run_sync(operation, *args)

    @staticmethod
    def overloaded() -> HTTPException:
        """统一公开过载响应，不将连接池异常或内部参数暴露给客户端。"""
        return HTTPException(
            503, detail={"code": "database_overloaded"}, headers={"Retry-After": "1"}
        )

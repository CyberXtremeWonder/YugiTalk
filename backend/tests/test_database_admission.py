"""数据库排队有界，超时、取消和异常不能泄漏工作许可。"""

import asyncio

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.admission import DatabaseAdmission
from app.core.config import Settings


def test_queue_limit_timeout_and_cancellation_return_capacity():
    """耗尽许可后只接纳有限等待者，拒绝、超时及取消后可继续服务。"""

    async def check():
        """在同一事件循环验证真实等待与取消，不依赖同步线程阻塞。"""
        gate = DatabaseAdmission(
            Settings(
                _env_file=None,
                api_database_concurrency=1,
                api_database_queue_limit=1,
                api_database_queue_timeout_seconds=0.03,
            )
        )

        async def wait():
            """申请一个被前面的请求持有的许可。"""
            async with gate.enter():
                return "entered"

        async with gate.enter():
            queued = asyncio.create_task(wait())
            await asyncio.sleep(0)
            assert gate.waiting == 1
            with pytest.raises(HTTPException) as rejected:
                async with gate.enter():
                    pytest.fail("queue must be bounded")
            assert rejected.value.status_code == 503
            assert rejected.value.headers == {"Retry-After": "1"}
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
            assert gate.waiting == 0
            with pytest.raises(HTTPException) as expired:
                await wait()
            assert expired.value.status_code == 503
        assert await wait() == "entered"
        assert gate.waiting == 0
        assert not gate.slots.locked()

    asyncio.run(check())


def test_synchronous_failure_returns_capacity():
    """SSE 短查询抛出异常后归还共享数据库许可。"""

    async def check():
        """异常与下一次查询使用同一个准入器。"""
        gate = DatabaseAdmission(Settings(_env_file=None, api_database_concurrency=1))

        def fail():
            """模拟在线程中发生的数据库异常。"""
            raise ValueError("query failed")

        with pytest.raises(ValueError, match="query failed"):
            await gate.run(fail)
        assert await gate.run(str, 42) == "42"

    asyncio.run(check())


def test_invalid_connection_budget_is_rejected():
    """配置不能允许数据库型请求并发超过该进程的连接池上限。"""
    with pytest.raises(ValidationError, match="API_DATABASE_CONCURRENCY"):
        Settings(_env_file=None, database_pool_size=1, database_max_overflow=0)


def test_reload_and_multiple_workers_are_mutually_exclusive():
    """避免启动器静默忽略多进程配置或同时启用热重载。"""
    with pytest.raises(ValidationError, match="APP_RELOAD"):
        Settings(_env_file=None, app_reload=True, app_workers=2)

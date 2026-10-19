"""真实检查必需依赖；检查失败不能返回虚假的就绪状态。"""

import asyncio
from collections.abc import Awaitable, Callable

import httpx
import psycopg
from redis.asyncio import Redis

from app.core.config import Settings
from app.health.milvus import MilvusClient

DependencyChecks = dict[str, bool]
HealthChecker = Callable[[], Awaitable[DependencyChecks]]


class DependencyChecker:
    """每次探测限定超时，短连接在探测结束后释放。"""

    def __init__(self, settings: Settings) -> None:
        """保存实例所需的依赖与配置，初始化后续执行使用的状态。"""
        self.settings = settings

    async def postgres(self) -> None:
        """验证关系数据库和迁移版本，向量存储由独立 Milvus 探测负责。"""
        async with await psycopg.AsyncConnection.connect(
            self.settings.database_url.get_secret_value()
        ) as connection:
            cursor = await connection.execute("SELECT version_num FROM alembic_version")
            version = await cursor.fetchone()
            if version is None or version == ("0001_vector",):
                raise RuntimeError("数据库迁移尚未完成")
            # 空表也可通过；尚未执行身份迁移时应拒绝就绪。
            await connection.execute("SELECT id FROM users LIMIT 1")
            await connection.execute("SELECT id FROM runs LIMIT 1")

    async def milvus(self) -> None:
        """使用有界请求探测向量数据库的实际可用状态。"""
        await MilvusClient(self.settings).ready()

    async def redis(self) -> None:
        """验证队列连接，不触发任何业务任务。"""
        async with Redis.from_url(self.settings.redis_url.get_secret_value()) as client:
            await client.ping()

    async def storage(self) -> None:
        """当前开发存储为 MinIO；后续存储适配器另行扩展。"""
        # 内部存储探测直连，避免系统代理将 localhost 或容器服务转发到外部。
        async with httpx.AsyncClient(
            timeout=self.settings.dependency_timeout_seconds, trust_env=False
        ) as client:
            result = await client.get(
                f"{self.settings.storage_endpoint.rstrip('/')}/minio/health/ready"
            )
            result.raise_for_status()

    async def _safe_check(self, probe: Callable[[], Awaitable[None]]) -> bool:
        """隔离单项依赖探测异常和超时，返回可公开的健康结果。"""
        try:
            await asyncio.wait_for(probe(), timeout=self.settings.dependency_timeout_seconds)
            return True
        except Exception:
            # 只返回状态，异常可能包含连接串与密码，不返回给客户端或普通日志。
            return False

    async def check(self) -> DependencyChecks:
        """并发检查依赖，总耗时受单次超时上限约束。"""
        probes = {
            "postgres": self.postgres,
            "redis": self.redis,
            "storage": self.storage,
            "milvus": self.milvus,
        }
        results = await asyncio.gather(*(self._safe_check(probe) for probe in probes.values()))
        return dict(zip(probes, results, strict=True))

"""真实检查必需依赖；检查失败不能返回虚假的就绪状态。"""

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import TypedDict

import httpx
import psycopg
from celery import Celery
from kombu import Connection  # type: ignore[import-untyped]
from redis.asyncio import Redis

from app.core.config import Settings
from app.health.milvus import MilvusClient

DependencyChecks = dict[str, bool]
HealthChecker = Callable[[], Awaitable[DependencyChecks]]


class DependencyDetail(TypedDict):
    """管理面板使用的单项探测结果，不包含连接信息或原始异常。"""

    ready: bool
    latency_ms: float | None
    checked_at: str
    description: str


DependencyDetails = dict[str, DependencyDetail]
DetailedHealthChecker = Callable[[], Awaitable[DependencyDetails]]


class _ProbeConnection(Connection):  # type: ignore[misc]
    """仅用于控制通道探测，禁止 Kombu 发布层自行无限重试。"""

    def ensure(
        self, obj: object, fun: Callable[..., object], **kwargs: object
    ) -> Callable[..., object]:
        """发布失败立即交给外层有界健康探测处理，不修改业务连接。"""
        return fun


class DependencyChecker:
    """每次探测限定超时，短连接在探测结束后释放。"""

    def __init__(self, settings: Settings) -> None:
        """保存实例所需的依赖与配置，初始化后续执行使用的状态。"""
        self.settings = settings
        self._worker_probe_lock = threading.Lock()

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

    async def docling(self) -> None:
        """只检查文档解析服务的健康端点，不提交文档转换。"""
        async with httpx.AsyncClient(
            timeout=self.settings.dependency_timeout_seconds, trust_env=False
        ) as client:
            result = await client.get(f"{self.settings.docling_url.rstrip('/')}/health")
            result.raise_for_status()

    def _embedding_health_url(self) -> str | None:
        """只对项目已知本地 BGE 部署使用健康协议，不猜测通用提供者路径。"""
        base = self.settings.embedding_base_url.rstrip("/")
        if self.settings.embedding_provider == "mock" or base not in {
            "http://embedding:8000/v1",
            "http://127.0.0.1:18081/v1",
            "http://localhost:18081/v1",
        }:
            return None
        return base.removesuffix("/v1") + "/health"

    async def embedding(self) -> None:
        """检查本地模型加载状态与模型、维度匹配，不发起嵌入推理。"""
        url = self._embedding_health_url()
        if url is None:
            raise RuntimeError("未配置已知本地嵌入健康端点")
        async with httpx.AsyncClient(
            timeout=self.settings.dependency_timeout_seconds, trust_env=False
        ) as client:
            result = await client.get(url)
            result.raise_for_status()
            payload = result.json()
        if not isinstance(payload, dict) or (
            payload.get("status") != "ok"
            or payload.get("model") != self.settings.embedding_model
            or payload.get("dimension") != self.settings.embedding_dimension
        ):
            raise RuntimeError("本地嵌入服务健康响应无效")

    def _worker_ping(self) -> None:
        """线程实际结束前只允许一个控制探测，取消异步等待不释放此锁。"""
        if not self._worker_probe_lock.acquire(blocking=False):
            raise RuntimeError("上一次 Worker 控制探测尚未结束")
        try:
            self._raw_worker_ping()
        finally:
            self._worker_probe_lock.release()

    def _raw_worker_ping(self) -> None:
        """限定连接、读写和回复超时，只验证至少一个 Worker 控制通道响应。"""
        timeout = self.settings.dependency_timeout_seconds
        broker = self.settings.redis_url.get_secret_value()
        with Celery("yugitalk-health", broker=broker, set_as_current=False) as app:
            app.conf.update(task_serializer="json", accept_content=["json"])
            # 控制广播必须使用下面的单次连接，不能从业务连接池借用带重试的发布器。
            app.control.mailbox.producer_pool = None
            with _ProbeConnection(
                broker,
                connect_timeout=timeout / 4,
                transport_options={
                    "socket_timeout": timeout / 4,
                    "socket_connect_timeout": timeout / 4,
                    "retry_on_timeout": False,
                    "max_retries": 0,
                },
            ) as connection:
                connection.ensure_connection(max_retries=0)
                replies = app.control.inspect(
                    connection=connection,
                    timeout=timeout / 2,
                    limit=1,
                    pattern="yugitalk@*",
                    matcher="glob",
                ).ping()
        if not isinstance(replies, dict) or not any(
            str(name).startswith("yugitalk@")
            and isinstance(reply, dict)
            and reply.get("ok") == "pong"
            for name, reply in replies.items()
        ):
            raise RuntimeError("未收到 Worker 控制通道回复")

    async def worker(self) -> None:
        """阻塞控制探测放到线程，外层单项超时不阻塞 API 事件循环。"""
        await asyncio.to_thread(self._worker_ping)

    async def _safe_check(self, probe: Callable[[], Awaitable[None]]) -> bool:
        """隔离单项依赖探测异常和超时，返回可公开的健康结果。"""
        try:
            await asyncio.wait_for(probe(), timeout=self.settings.dependency_timeout_seconds)
            return True
        except Exception:
            # 只返回状态，异常可能包含连接串与密码，不返回给客户端或普通日志。
            return False

    async def check(self) -> DependencyChecks:
        """保持就绪接口的布尔契约，复用有界的实际依赖探测。"""
        return {name: detail["ready"] for name, detail in (await self.check_detailed()).items()}

    async def _check_detail(
        self, probe: Callable[[], Awaitable[None]], description: str
    ) -> DependencyDetail:
        """记录实际探测耗时和完成时间，失败仍只公开固定检查说明。"""
        started = time.monotonic()
        ready = await self._safe_check(probe)
        return {
            "ready": ready,
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
            "checked_at": datetime.now(UTC).isoformat(),
            "description": description,
        }

    async def check_detailed(self) -> DependencyDetails:
        """并发检查依赖，总耗时受单次超时上限约束。"""
        return await self._check_probes(
            {
                "postgres": (self.postgres, "检查数据库迁移版本及身份、任务表可读性"),
                "redis": (self.redis, "执行 Redis PING，检查队列连接"),
                "storage": (self.storage, "检查 MinIO 存储就绪端点"),
                "milvus": (self.milvus, "读取集合列表，检查向量数据库及凭证可用性"),
            }
        )

    async def check_admin_detailed(self) -> DependencyDetails:
        """管理面板额外探测解析与工作进程，不改变公共就绪接口的核心依赖集合。"""
        probes = {
            "docling": (self.docling, "检查 Docling 解析服务健康端点，不执行转换"),
            "worker": (self.worker, "至少一个本项目 Worker 控制通道响应，不代表业务任务执行成功"),
        }
        if self._embedding_health_url() is not None:
            probes["embedding"] = (
                self.embedding,
                "检查本地 BGE 模型加载及模型、维度匹配，不执行推理",
            )
        core, extra = await asyncio.gather(self.check_detailed(), self._check_probes(probes))
        return {**core, **extra}

    async def _check_probes(
        self, probes: Mapping[str, tuple[Callable[[], Awaitable[None]], str]]
    ) -> DependencyDetails:
        """一次并发完成给定探测集合，逐项隔离失败。"""
        results = await asyncio.gather(
            *(self._check_detail(probe, description) for probe, description in probes.values())
        )
        # 单个依赖异常被转换为 False，其余探测仍完成；结果顺序与 probes 插入顺序一致。
        return dict(zip(probes, results, strict=True))

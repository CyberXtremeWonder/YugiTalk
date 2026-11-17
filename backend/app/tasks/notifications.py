"""Redis 只唤醒订阅者，事件内容与续传游标仍由数据库提供。"""

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from weakref import WeakKeyDictionary

from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from redis.asyncio.retry import Retry as AsyncRetry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session

from app.core.config import Settings

CHANNEL = "yugitalk:run-events"
PENDING = "yugitalk_run_notifications"
logger = logging.getLogger(__name__)


class EventPublisher:
    """复用进程内 Redis 连接，通知失败不会撤销已经提交的业务事务。"""

    def __init__(self, settings: Settings) -> None:
        """延迟建连，避免 Worker fork 前创建套接字。"""
        self.url = settings.redis_url.get_secret_value()
        self.client: Redis | None = None
        self.pid = os.getpid()
        self.retry_after = 0.0
        self.lock = threading.Lock()

    def publish(self, keys: set[str]) -> None:
        """只发送任务编号；短超时与失败冷却限制 Redis 故障的额外开销。"""
        with self.lock:
            if self.pid != os.getpid():
                self.client, self.pid = None, os.getpid()
                self.retry_after = 0.0
            if time.monotonic() < self.retry_after:
                return
            if self.client is None:
                self.client = Redis.from_url(
                    self.url,
                    socket_timeout=0.1,
                    socket_connect_timeout=0.1,
                    max_connections=4,
                    retry=Retry(NoBackoff(), 0),
                )
            client = self.client
        try:
            client.publish(CHANNEL, json.dumps(sorted(keys)))
        except RedisError:
            with self.lock:
                self.retry_after = time.monotonic() + 5
            logger.warning("事件通知暂不可用，订阅者将从数据库补读")

    def close(self, _: Engine) -> None:
        """只关闭当前进程创建的连接，不复用 fork 继承的客户端。"""
        if self.client is not None and self.pid == os.getpid():
            self.client.close()
        self.client = None


publishers: WeakKeyDictionary[Engine, EventPublisher] = WeakKeyDictionary()


def register_publisher(engine: Engine, settings: Settings) -> None:
    """按引擎注册配置，隔离不同数据库与测试实例，不读取全局环境。"""
    publisher = EventPublisher(settings)
    publishers[engine] = publisher
    event.listen(engine, "engine_disposed", publisher.close)


def notify_after_commit(db: Session, key: str) -> None:
    """同一事务合并任务编号，回滚不发布，嵌套提交等待最外层提交。"""
    if PENDING not in db.info:
        db.info[PENDING] = set()
    db.info[PENDING].add(key)
    if not event.contains(db, "after_commit", committed):
        event.listen(db, "after_commit", committed)
        event.listen(db, "after_rollback", rolled_back)


def committed(db: Session) -> None:
    """提交成功后尽力通知；通知丢失由订阅端的有界补读恢复。"""
    if db.in_nested_transaction():
        return
    keys = db.info.pop(PENDING, set())
    bind = db.get_bind()
    engine = bind if isinstance(bind, Engine) else bind.engine
    publisher = publishers.get(engine)
    if keys and publisher is not None:
        publisher.publish(keys)


def rolled_back(db: Session) -> None:
    """丢弃回滚事务的通知；漏掉的唤醒不会丢失已提交事件。"""
    db.info.pop(PENDING, None)


class EventHub:
    """每个 API 进程只用一个 Redis 订阅连接，向本地对应任务广播唤醒。"""

    def __init__(self, settings: Settings) -> None:
        """保存连接配置，订阅集合只在应用事件循环内修改。"""
        self.url = settings.redis_url.get_secret_value()
        self.listeners: dict[str, set[asyncio.Event]] = {}
        self.task: asyncio.Task[None] | None = None
        self.connected = asyncio.Event()

    def start(self) -> None:
        """应用生命周期启动订阅，不阻塞 API 的数据库能力。"""
        self.task = asyncio.create_task(self.listen())

    async def close(self) -> None:
        """取消订阅任务，等待 Redis 上下文关闭连接。"""
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    @contextmanager
    def subscribe(self, key: str) -> Iterator[asyncio.Event]:
        """先登记再读库，防止读库和开始等待之间遗漏通知。"""
        changed = asyncio.Event()
        self.listeners.setdefault(key, set()).add(changed)
        try:
            yield changed
        finally:
            self.listeners[key].remove(changed)
            if not self.listeners[key]:
                del self.listeners[key]

    def wake(self, keys: list[str]) -> None:
        """合并重复通知，不缓存正文，不给每个客户端创建 Redis 连接。"""
        for key in keys:
            for changed in self.listeners.get(key, ()):
                changed.set()

    async def listen(self) -> None:
        """网络恢复后自动重订阅；断连期间 SSE 仍定期鉴权并补读数据库。"""
        while True:
            try:
                async with (
                    AsyncRedis.from_url(
                        self.url,
                        socket_connect_timeout=0.2,
                        socket_timeout=2,
                        max_connections=1,
                        retry=AsyncRetry(NoBackoff(), 0),
                    ) as client,
                    client.pubsub() as channel,
                ):
                    await channel.subscribe(CHANNEL)
                    while True:
                        message = await channel.get_message(timeout=1)
                        if message is None:
                            continue
                        if message["type"] == "subscribe":
                            self.connected.set()
                        elif message["type"] == "message":
                            data = message["data"]
                            if len(data) > 65536:
                                continue
                            try:
                                keys = json.loads(data)
                            except (ValueError, UnicodeDecodeError):
                                continue
                            if isinstance(keys, list) and all(isinstance(k, str) for k in keys):
                                self.wake(keys)
            except (RedisError, OSError):
                self.connected.clear()
                await asyncio.sleep(1)

"""通知只在提交后唤醒，丢失通知与权限撤销不会泄露或丢失事件。"""

import asyncio
from uuid import UUID

import pytest
from redis.exceptions import ConnectionError
from sqlalchemy import create_engine, delete, text
from sqlalchemy.orm import Session
from starlette.requests import Request
from test_agents import setup_task

from app.identity.dependencies import authenticated
from app.identity.models import LoginSession
from app.tasks.execution import append_event, finish_run
from app.tasks.models import Run
from app.tasks.notifications import (
    EventPublisher,
    notify_after_commit,
    publishers,
)
from app.tasks.router import events

pytest_plugins = ["test_identity"]


def test_notifications_wait_for_outer_commit_and_ignore_rollback(monkeypatch):
    """回滚和保存点提交都不能提前发布事件，最外层提交合并同一任务。"""
    engine = create_engine("sqlite://")
    sent = []

    class Publisher:
        """保存实际发布的编号，不连接 Redis。"""

        def publish(self, keys):
            """复制通知，防止后续集合修改影响断言。"""
            sent.append(set(keys))

    monkeypatch.setitem(publishers, engine, Publisher())
    with Session(engine) as db:
        db.execute(text("SELECT 1"))
        notify_after_commit(db, "rolled-back")
        db.rollback()
        assert sent == []
        with db.begin():
            notify_after_commit(db, "run-a")
            with db.begin_nested():
                notify_after_commit(db, "run-a")
            assert sent == []
            notify_after_commit(db, "run-b")
        assert sent == [{"run-a", "run-b"}]
        db.commit()
        assert len(sent) == 1
    engine.dispose()


def test_redis_failure_is_best_effort_and_has_cooldown():
    """通知连接断开不抛出业务异常，也不在每次提交时重复尝试。"""
    from app.core.config import Settings

    attempted = []

    class BrokenRedis:
        """模拟 Redis 连接不可用。"""

        def publish(self, *args):
            """记录尝试次数并返回网络错误。"""
            attempted.append(args)
            raise ConnectionError("isolated failure")

    publisher = EventPublisher(Settings(_env_file=None))
    publisher.client = BrokenRedis()
    publisher.publish({"run-a"})
    publisher.publish({"run-b"})
    assert len(attempted) == 1


def stream_for(client, task):
    """创建真实权限与事件读取逻辑的流迭代器，不启动额外 HTTP 客户端。"""

    async def receive():
        """模拟仍保持连接的客户端，可被断连检测的取消作用域中断。"""
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    cookie = "; ".join(f"{key}={value}" for key, value in client.cookies.items())
    request = Request(
        {
            "type": "http",
            "app": client.app,
            "headers": [(b"cookie", cookie.encode())],
        },
        receive,
    )
    with Session(client.app.state.database) as db:
        actor = authenticated(request, db)
        response = events(UUID(task["id"]), request, db, actor, None, None)
    return response.body_iterator


def test_notification_wakes_stream_without_periodic_database_poll(client, monkeypatch):
    """空闲时不频繁查库，通知唤醒后仍从数据库取事件并推进游标。"""
    _, _, task, _ = setup_task(client)
    client.app.state.settings.run_event_fallback_seconds = 30
    from app.tasks import router

    original = router.poll_events
    calls = []

    def counted(*args):
        """记录真实读取次数，不替换权限检查或数据库结果。"""
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(router, "poll_events", counted)

    async def check():
        """让流停在等待通知处，再提交新事件并唤醒。"""
        stream = stream_for(client, task)
        assert await anext(stream) == ": heartbeat\n\n"
        reading = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.05)
        assert len(calls) == 2
        with Session(client.app.state.database) as db:
            append_event(db, db.get(Run, task["id"]), "text_delta", {"text": "committed"})
            db.commit()
        client.app.state.event_hub.wake([task["id"]])
        frame = await asyncio.wait_for(reading, 1)
        assert "event: text_delta" in frame and "committed" in frame
        assert f"id: {task['id']}:1" in frame
        await stream.aclose()
        assert client.app.state.event_hub.listeners == {}

    asyncio.run(check())


def test_lost_notification_recovers_terminal_event(client):
    """完全不发送 Redis 通知也能补读正文及终态，并在结束时注销订阅。"""
    _, _, task, _ = setup_task(client)
    client.app.state.settings.run_event_fallback_seconds = 0.1

    async def check():
        """首轮读取后提交终态，下一轮只依靠超时补读。"""
        stream = stream_for(client, task)
        assert await anext(stream) == ": heartbeat\n\n"
        assert finish_run(
            client.app.state.database, task["id"], "succeeded", None, "", 0, None, None
        )
        frame = await asyncio.wait_for(anext(stream), 1)
        assert "event: completed" in frame
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert client.app.state.event_hub.listeners == {}

    asyncio.run(check())


def test_revoked_session_cannot_receive_notified_content(client):
    """会话撤销后即使收到正文通知，也必须重新鉴权并关闭流。"""
    _, _, task, _ = setup_task(client)

    async def check():
        """先建流再撤销登录会话，验证通知不能绕过数据库权限。"""
        stream = stream_for(client, task)
        assert await anext(stream) == ": heartbeat\n\n"
        with Session(client.app.state.database) as db:
            db.execute(delete(LoginSession))
            append_event(db, db.get(Run, task["id"]), "text_delta", {"text": "private"})
            db.commit()
        client.app.state.event_hub.wake([task["id"]])
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), 1)
        assert client.app.state.event_hub.listeners == {}

    asyncio.run(check())

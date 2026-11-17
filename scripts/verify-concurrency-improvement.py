"""复用前测发压函数，对照改进后的普通 API 和真实 Redis SSE。"""
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/concurrency-improvement-20261108"
spec = importlib.util.spec_from_file_location("baseline", ROOT / "scripts/simulate-peak-baseline.py")
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)
peak = baseline.peak
peak.OUT, peak.COMPOSE = OUT, OUT / "compose.yml"
peak.BASE = "http://127.0.0.1:48081"
peak.DB_URL = "postgresql://peak_test:isolated-peak-test-only@127.0.0.1:55434/peak_test"
baseline.NAME = "yugitalk-concurrency-improvement-20261108-api-1"


async def sse_probe(fixtures, admin):
    """跨进程通知、无通知补读、重连续传与撤权都走真实 HTTP 和数据库。"""
    sys.path.insert(0, str(ROOT / "backend"))
    from sqlalchemy import delete, select
    from sqlalchemy.orm import Session
    from app.agents.models import Agent, AgentVersion
    from app.chat.models import Conversation
    from app.core.config import Settings
    from app.core.database import create_database
    from app.identity.models import LoginSession, User
    from app.tasks.execution import append_event
    from app.tasks.models import Run

    engine = create_database(Settings(_env_file=None, app_env="test", database_url=peak.DB_URL,
                                     redis_url="redis://127.0.0.1:56380/0"))
    runs = []
    with Session(engine) as db:
        for i in range(100):
            user = db.scalar(select(User).where(User.email == f"peak-{i}@example.invalid"))
            agent = Agent(account_id=user.id, name="Isolated SSE fixture")
            db.add(agent)
            db.flush()
            version = AgentVersion(agent_id=agent.id, number=1, config={})
            db.add(version)
            db.flush()
            conversation = db.scalar(select(Conversation).where(Conversation.owner_id == user.id))
            run = Run(account_id=user.id, owner_id=user.id, conversation_id=conversation.id,
                      agent_version_id=version.id, snapshot={}, idempotency_key=str(uuid4()),
                      request_hash="0"*64, prompt="Isolated SSE fixture", status="running", reserved_tokens=0)
            db.add(run)
            db.flush()
            runs.append({"id": run.id, "owner": user.id})
        db.commit()

    async def metrics():
        """等待测试应用启动，不绕过真实生命周期中的 Redis 订阅。"""
        return await baseline.snapshot(admin)

    for _ in range(60):
        try:
            if (await metrics())["redis_connected"]:
                break
        except (aiohttp.ClientError, asyncio.TimeoutError):
            pass
        await asyncio.sleep(.2)
    else:
        raise RuntimeError("redis_subscriber_not_ready")
    async with admin.post(peak.BASE+"/__peak/count-sql?enabled=true", headers=peak.CONTROL) as r:
        r.raise_for_status()
    received = [[] for _ in runs]
    heartbeats = [asyncio.Event() for _ in runs]
    observed = [asyncio.Event() for _ in runs]

    async def read(client, i, last=0):
        """按 SSE 帧记录事件，复连使用服务端现有 Last-Event-ID。"""
        headers = {"Cookie": fixtures[i]["cookie"]}
        if last:
            headers["Last-Event-ID"] = f"{runs[i]['id']}:{last}"
        async with client.get(peak.BASE+f"/api/v1/runs/{runs[i]['id']}/events", headers=headers) as r:
            assert r.status == 200, (i, r.status, await r.text())
            frame = []
            async for raw in r.content:
                line = raw.decode().strip()
                if line.startswith(": heartbeat"):
                    heartbeats[i].set()
                elif line:
                    frame.append(line)
                elif frame:
                    data = json.loads(next(s[6:] for s in frame if s.startswith("data: ")))
                    received[i].append(data)
                    observed[i].set()
                    frame.clear()

    async def wait_sequence(sequence, indices):
        """等待所选订阅者收到指定序号，不以客户端连接数替代事件交付。"""
        async def one(i):
            """通知与检查之间无 await，避免漏掉唤醒。"""
            while not any(e["sequence"] == sequence for e in received[i]):
                observed[i].clear()
                await observed[i].wait()
        await asyncio.wait_for(asyncio.gather(*(one(i) for i in indices)), 12)

    def emit(sequence, terminal=False, revoke=False):
        """Windows 生产者提交后经 Redis 唤醒 Linux API，通知中没有正文。"""
        with Session(engine) as db:
            if revoke:
                db.execute(delete(LoginSession).where(LoginSession.user_id == runs[0]["owner"]))
            for row in runs:
                run = db.get(Run, row["id"])
                if terminal:
                    run.status = "succeeded"
                append_event(db, run, "completed" if terminal else "text_delta",
                             {"status": "succeeded"} if terminal else {"text": f"fragment-{sequence}"})
            db.commit()

    report = {"subscribers": 100, "scope": "Real PostgreSQL/Redis, Windows producer and Linux API, no model calls"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_read=15), trust_env=False) as client:
        tasks = []
        try:
            for i in range(100):
                tasks.append(asyncio.create_task(read(client, i)))
                await asyncio.sleep(.01)
            await asyncio.wait_for(asyncio.gather(*(e.wait() for e in heartbeats)), 10)
            before = await metrics()
            await asyncio.sleep(2)
            after = await metrics()
            report["idle_two_seconds_sql_delta"] = after["queries"] - before["queries"]
            report["idle_checked_out"] = after["checked_out"]
            assert after["subscribers"] == 100 and after["checked_out"] == 0
            started = time.perf_counter()
            await asyncio.to_thread(emit, 1)
            await wait_sequence(1, range(100))
            report["notification_all_100_delivery_seconds"] = time.perf_counter()-started
            for task in tasks[:5]:
                task.cancel()
            await asyncio.gather(*tasks[:5], return_exceptions=True)
            await asyncio.to_thread(emit, 2)
            await wait_sequence(2, range(5,100))
            for i in range(5):
                tasks[i] = asyncio.create_task(read(client, i, last=1))
            await wait_sequence(2, range(5))
            report["five_reconnections_replayed_sequence_two"] = True
            await asyncio.to_thread(peak.docker, "stop", "redis")
            started = time.perf_counter()
            await asyncio.to_thread(emit, 3)
            await wait_sequence(3, range(100))
            report["redis_outage_all_100_delivery_seconds"] = time.perf_counter()-started
            await asyncio.to_thread(peak.docker, "start", "redis")
            for _ in range(100):
                if (await metrics())["redis_connected"]:
                    break
                await asyncio.sleep(.1)
            else:
                raise RuntimeError("redis_subscriber_not_reconnected")
            # 发布者也有 5 秒故障冷却；恢复后先等冷却结束再验证通知时延。
            await asyncio.sleep(5)
            started = time.perf_counter()
            await asyncio.to_thread(emit, 4)
            await wait_sequence(4, range(100))
            report["redis_recovery_all_100_delivery_seconds"] = time.perf_counter()-started
            await asyncio.to_thread(emit, 5, revoke=True)
            await wait_sequence(5, range(1,100))
            await asyncio.wait_for(asyncio.shield(tasks[0]), 10)
            assert not any(e["sequence"] >= 5 for e in received[0])
            report["revoked_session_received_no_new_content"] = True
            await asyncio.to_thread(emit, 6, terminal=True)
            await asyncio.wait_for(asyncio.gather(*tasks[1:]), 12)
            for i, events in enumerate(received):
                sequences = [e["sequence"] for e in events]
                assert sequences == list(range(1,5 if i==0 else 7)), (i,sequences)
            report["duplicate_or_missing_events"] = 0
            for _ in range(50):
                final = await metrics()
                if final["subscribers"] == 0 and final["checked_out"] == 0:
                    break
                await asyncio.sleep(.1)
            assert final["subscribers"] == 0 and final["checked_out"] == 0
            report["final"] = final
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            engine.dispose()
    async with admin.post(peak.BASE+"/__peak/count-sql?enabled=false", headers=peak.CONTROL) as r:
        r.raise_for_status()
    peak.save("sse-probe.json", report)
    print(json.dumps(report), flush=True)


async def main():
    """新数据库与新结果目录保留改前报告，失败时不覆盖已有结论。"""
    assert not (OUT/"manifest.json").exists(), "Preserve existing verification"
    files = [ROOT/p for p in ("backend/app/core/admission.py", "backend/app/core/database.py",
             "backend/app/identity/dependencies.py", "backend/app/tasks/notifications.py",
             "backend/app/tasks/execution.py", "backend/app/tasks/router.py", "backend/app/main.py",
             "backend/app/core/config.py", "scripts/verify-concurrency-improvement.py")]
    peak.save("manifest.json", {"api_processes":1,"api_cpus":2,"api_memory_gib":2,
              "pool_size":5,"max_overflow":10,"pool_timeout_seconds":5,
              "database_concurrency":10,"queue_limit":100,"queue_timeout_seconds":1,
              "client":"Same native Windows aiohttp and request function as prior baseline",
              "source_hashes":{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}})
    rows = await asyncio.to_thread(peak.seed)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5), trust_env=False) as admin:
        await sse_probe(rows, admin)
        reports=[]
        # 第一个用户在 SSE 撤权测试中失效；重新创建有效会话用于独立普通 API 轮次。
        rows = await asyncio.to_thread(baseline.fixtures)
        for name,users,span in (("improved-paced-1000",1000,30),("improved-paced-10000",10000,30),
                                ("improved-burst-1000",1000,0),("improved-burst-10000",10000,0)):
            reports.append(await baseline.phase(name,users,span,rows,admin))
            peak.save("summary.json",[{k:v for k,v in r.items() if k not in ("results","samples")} for r in reports])


if __name__ == "__main__":
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = ""
    os.environ["OTEL_SAMPLE_RATIO"] = "0"
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())

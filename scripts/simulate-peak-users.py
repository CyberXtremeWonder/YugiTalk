"""Run 1k/10k authenticated users against a dedicated current-code API and database."""
import asyncio
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import aiohttp
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/peak-users-20261108"
COMPOSE = OUT / "compose.yml"
BASE = "http://127.0.0.1:48080"
SECRET = "isolated-peak-session-secret-20261108"
CONTROL = {"X-Peak-Key": "isolated-peak-control-20261108"}
DB_URL = "postgresql://peak_test:isolated-peak-test-only@127.0.0.1:55433/peak_test"


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def docker(*args):
    return subprocess.run(["docker", "compose", "-f", str(COMPOSE), *args], check=True, capture_output=True, text=True).stdout


def seed():
    sys.path.insert(0, str(ROOT / "backend"))
    from sqlalchemy import insert
    from app.core.database import create_database
    from app.core.config import Settings
    from app.core.entities import Base, now
    from app.identity.models import User, LoginSession
    from app.identity.security import digest
    from app.chat.models import Conversation, Message

    engine = create_database(Settings(_env_file=None, app_env="test", database_url=DB_URL))
    Base.metadata.create_all(engine)
    fixtures, users, sessions, conversations, messages = [], [], [], [], []
    for i in range(10000):
        uid, cid, token = str(uuid4()), str(uuid4()), secrets.token_urlsafe(32)
        fixtures.append({"id": uid, "conversation": cid, "cookie": "yugitalk_session=" + token, "csrf": digest(SECRET, "csrf", token)})
        users.append({"id": uid, "email": f"peak-{i}@example.invalid", "name": f"Peak user {i}", "password_hash": "disabled-fixture-password", "active": True, "platform_admin": False})
        sessions.append({"token_hash": digest(SECRET, "session", token), "user_id": uid, "expires_at": now() + timedelta(hours=2), "development_mode": False})
        conversations.append({"id": cid, "account_id": uid, "owner_id": uid, "title": f"Peak fixture {i}", "created_at": now()})
        messages.append({"id": str(uuid4()), "conversation_id": cid, "sequence": 1, "role": "user", "content": "Isolated load fixture", "sources": [], "created_at": now()})
    with engine.begin() as conn:
        for model, rows in ((User, users), (LoginSession, sessions), (Conversation, conversations), (Message, messages)):
            for start in range(0, len(rows), 500):
                conn.execute(insert(model), rows[start:start + 500])
    engine.dispose()
    return fixtures


async def control(session, path="metrics", method="GET"):
    async with session.request(method, BASE + "/__peak/" + path, headers=CONTROL, timeout=aiohttp.ClientTimeout(total=5)) as response:
        response.raise_for_status()
        return await response.json()


async def ready(session):
    for _ in range(60):
        try:
            return await control(session)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep(1)
    raise RuntimeError("isolated_api_not_ready")


async def reset_control(session):
    for _ in range(100):
        m = await control(session)
        if not m['checked_out'] and not m['active']:
            return await control(session, 'reset', 'POST')
        await asyncio.sleep(.05)
    raise RuntimeError('warmup_connections_not_returned')


async def request(session, fixture, phase, number, due, operation="mixed", hold_seconds=0):
    await asyncio.sleep(max(0, due - time.perf_counter()))
    started = time.perf_counter()
    headers = {"Cookie": fixture["cookie"], "Origin": BASE, "X-CSRF-Token": fixture["csrf"]}
    method, path, body = "GET", "/api/v1/me/conversations", None
    kind = "read"
    if operation == "hold":
        path, kind = f"/__peak/hold?seconds={hold_seconds}", "injected_slow_sql"
        headers.update(CONTROL)
    elif operation == "mixed" and number % 5 == 0:
        method, path, body, kind = "PATCH", "/api/v1/conversations/" + fixture["conversation"], {"title": f"{phase}-{number}"}, "rename"
    result = {"number": number, "user_index": number % 10000, "operation": kind, "schedule_lag_ms": (started - due) * 1000}
    try:
        async with session.request(method, BASE + path, headers=headers, json=body) as response:
            data = await response.json(content_type=None)
            result["status"] = response.status
            result["server_ms"] = float(response.headers["X-Peak-Server-Ms"]) if "X-Peak-Server-Ms" in response.headers else None
            if response.status == 200:
                result["validated"] = data.get("ok") is True if kind == "injected_slow_sql" else (data.get("id") == fixture["conversation"] and data.get("title") == body["title"]) if kind == "rename" else len(data.get("items", [])) == 1 and data["items"][0]["id"] == fixture["conversation"]
            else:
                detail = data.get("detail")
                result["error_code"] = data.get("code") or (detail.get("code") if isinstance(detail, dict) else None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError) as error:
        result["client_error"] = type(error).__name__
    result["seconds"] = time.perf_counter() - started
    result["arrival_to_completion_seconds"] = time.perf_counter() - due
    return result


def percentiles(values):
    return {"p50": float(np.percentile(values, 50)), "p95": float(np.percentile(values, 95)), "p99": float(np.percentile(values, 99)), "max": max(values)} if values else None


async def phase(name, users, span, fixtures, admin):
    await asyncio.to_thread(docker, "restart", "api")
    await ready(admin)
    connector = aiohttp.TCPConnector(limit=10000, limit_per_host=10000, keepalive_timeout=60)
    timeout = aiohttp.ClientTimeout(total=75, sock_connect=15, sock_read=65)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout, trust_env=False) as session:
        # Warm all routes without including warmup in measurements.
        for n in range(40):
            row = await request(session, fixtures[n], "warmup", n, time.perf_counter())
            assert row.get("validated"), row
        await reset_control(admin)
        samples, results = [], []
        started = time.perf_counter()
        active, peak_active = 0, 0
        async def run(n):
            nonlocal active, peak_active
            due = started + n * span / users if span else started
            await asyncio.sleep(max(0, due - time.perf_counter()))
            active += 1
            peak_active = max(active, peak_active)
            try:
                return await request(session, fixtures[n], name, n, due)
            finally:
                active -= 1
        tasks = [asyncio.create_task(run(n)) for n in range(users)]
        async def monitor():
            while any(not t.done() for t in tasks):
                try:
                    m = await control(admin)
                    samples.append({"elapsed_seconds": time.perf_counter() - started, **{k:m[k] for k in ("active", "peak_active", "checked_out", "peak_checked_out", "pool_timeouts")}})
                    if len(samples) % 3 == 0:
                        def resources():
                            r = subprocess.run(['docker', 'stats', '--no-stream', '--format', '{{json .}}', 'yugitalk-peak-users-20261108-api-1', 'yugitalk-peak-users-20261108-postgres-1'], capture_output=True, text=True, timeout=10)
                            return [json.loads(line) for line in r.stdout.splitlines() if line.startswith('{')]
                        sample = {'phase': name, 'elapsed_seconds': time.perf_counter() - started, 'containers': await asyncio.to_thread(resources)}
                        with (OUT/'resources.jsonl').open('a', encoding='utf-8') as f:
                            f.write(json.dumps(sample) + '\n')
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    samples.append({"elapsed_seconds": time.perf_counter() - started, "monitor_unavailable": True})
                await asyncio.sleep(1)
        watcher = asyncio.create_task(monitor())
        for future in asyncio.as_completed(tasks):
            results.append(await future)
        elapsed = time.perf_counter() - started
        await watcher
        drained = False
        final = None
        drain_started = time.perf_counter()
        while time.perf_counter() - drain_started < 30:
            try:
                final = await control(admin)
                if not final['active'] and not final['checked_out']:
                    drained = True
                    break
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(1)
        if final is not None:
            try:
                final = await control(admin, "metrics?detail=true")
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
    successes = [r for r in results if r.get("status") == 200 and r.get("validated")]
    report = {"name": name, "virtual_users": users, "requests": len(results), "arrival_window_seconds": span, "offered_rps": users/span if span else None, "elapsed_seconds": elapsed, "completed_rps": len(results)/elapsed, "successful_rps": len(successes)/elapsed, "client_peak_outstanding": peak_active, "statuses": dict(Counter(str(r.get('status')) for r in results if 'status' in r)), "client_errors": dict(Counter(r['client_error'] for r in results if 'client_error' in r)), "successes": len(successes), "response_validation_failures": sum(r.get('status') == 200 and not r.get('validated') for r in results), "latency_all_seconds": percentiles([r['seconds'] for r in results]), "latency_success_seconds": percentiles([r['seconds'] for r in successes]), "server_success_ms": percentiles([r['server_ms'] for r in successes if r.get('server_ms') is not None]), "generator_schedule_lag_ms": percentiles([r['schedule_lag_ms'] for r in results]), "drained_within_30s_after_clients": drained, "pool": final, "samples": samples, "results": sorted(results, key=lambda r:r['number'])}
    save(name + ".json", report)
    print(json.dumps({k:report[k] for k in ("name", "successes", "requests", "elapsed_seconds", "statuses", "client_errors", "client_peak_outstanding", "drained_within_30s_after_clients")}), flush=True)
    return report


async def pool_probe(fixtures, admin):
    await asyncio.to_thread(docker, "restart", "api")
    await ready(admin)
    await reset_control(admin)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=65), connector=aiohttp.TCPConnector(limit=30), trust_env=False) as session:
        start = time.perf_counter()
        held = [asyncio.create_task(request(session, fixtures[n], "pool_probe", n, start, "hold", 35)) for n in range(15)]
        for _ in range(100):
            m = await control(admin)
            if m['checked_out'] == 15:
                break
            await asyncio.sleep(.1)
        else:
            raise RuntimeError("could_not_fill_15_connections")
        waiting = await request(session, fixtures[16], "pool_probe", 16, time.perf_counter(), "read")
        holders = await asyncio.gather(*held)
        recovery = await request(session, fixtures[17], "pool_recovery", 17, time.perf_counter(), "read")
        m = await control(admin, "metrics?detail=true")
    result = {"scope": "Injected pg_sleep(35), not normal endpoint performance", "holders": holders, "waiting_business_request": waiting, "recovery_request": recovery, "metrics": m}
    save("pool-exhaustion.json", result)
    print(json.dumps({"pool_probe_status": waiting.get('status'), "wait_seconds": waiting['seconds'], "recovery_status": recovery.get('status')}), flush=True)


async def main():
    assert not (OUT / "summary.json").exists(), "Preserve completed reports"
    fixtures = await asyncio.to_thread(seed)
    save("manifest.json", {"scope": "Current-code authenticated conversation listing/rename; preseeded logged-in sessions; no password login storm, worker tasks, model generation, SSE or RAG", "users": 10000, "api_processes": 1, "api_cpus": 2, "api_memory_gib": 2, "postgres_cpus": 2, "postgres_memory_gib": 2, "pool_size": 5, "max_overflow": 10, "pool_timeout_seconds": 30, "http": "Host loopback HTTP to isolated Linux Docker API; not HTTPS", "client": "Native Windows aiohttp, max 10000 connections, 75s total/15s connect timeouts; generator lag reported", "read_write_mix": "80% list conversations, 20% rename own conversation", "fixture": "10000 users, valid independently hashed sessions, one conversation and one message each; ORM create_all, not a migration validation", "source_hashes": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'backend/app/core/database.py', ROOT/'backend/app/chat/router.py', ROOT/'backend/app/identity/dependencies.py', OUT/'peak_server.py', Path(__file__))}})
    async with aiohttp.ClientSession(headers=CONTROL, timeout=aiohttp.ClientTimeout(total=5), trust_env=False) as admin:
        await ready(admin)
        await pool_probe(fixtures, admin)
        reports = []
        for name, users, span in (("paced-1000",1000,30), ("paced-10000",10000,30), ("burst-1000",1000,0), ("burst-10000",10000,0)):
            reports.append(await phase(name, users, span, fixtures, admin))
            save("summary.json", [{k:r[k] for k in ("name", "virtual_users", "requests", "offered_rps", "elapsed_seconds", "successes", "successful_rps", "statuses", "client_errors", "client_peak_outstanding", "latency_success_seconds", "generator_schedule_lag_ms", "drained_within_30s_after_clients")} for r in reports])


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())

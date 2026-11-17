"""Control run using the identical Windows client and unwrapped current app."""
import asyncio
from collections import Counter
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import secrets
import subprocess
import sys
import time

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("peak", ROOT / "scripts/simulate-peak-users.py")
peak = importlib.util.module_from_spec(spec)
spec.loader.exec_module(peak)
NAME = "yugitalk-peak-users-20261108-baseline"


def fixtures():
    sys.path.insert(0, str(ROOT / "backend"))
    from sqlalchemy import select, insert
    from app.core.config import Settings
    from app.core.database import create_database
    from app.core.entities import now
    from app.identity.models import User, LoginSession
    from app.identity.security import digest
    from app.chat.models import Conversation
    engine = create_database(Settings(_env_file=None, app_env="test", database_url=peak.DB_URL))
    result, sessions = [], []
    with engine.begin() as db:
        ids = {email: (uid, cid) for email, uid, cid in db.execute(select(User.email, User.id, Conversation.id).join(Conversation, Conversation.owner_id == User.id))}
        for i in range(10000):
            uid, cid = ids[f"peak-{i}@example.invalid"]
            token = secrets.token_urlsafe(32)
            result.append({"conversation": cid, "cookie": "yugitalk_session=" + token, "csrf": digest(peak.SECRET, "csrf", token)})
            sessions.append({"token_hash": digest(peak.SECRET, "session", token), "user_id": uid, "expires_at": now()+timedelta(hours=2), "development_mode": False})
        db.execute(insert(LoginSession), sessions)
    engine.dispose()
    return result


async def snapshot(admin, stacks=False):
    async with admin.get(peak.BASE + "/__peak/baseline", params={"stacks": str(stacks).lower()}, headers=peak.CONTROL) as response:
        response.raise_for_status()
        return await response.json()


async def phase(name, users, span, rows, admin):
    await asyncio.to_thread(subprocess.run, ["docker", "restart", NAME], check=True, capture_output=True)
    for _ in range(60):
        try:
            await snapshot(admin)
            break
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep(1)
    else:
        raise RuntimeError("baseline_not_ready")
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=10000, limit_per_host=10000, keepalive_timeout=60),
            timeout=aiohttp.ClientTimeout(total=75, sock_connect=15, sock_read=65), trust_env=False) as client:
        for i in range(40):
            assert (await peak.request(client, rows[i], "warmup", i, time.perf_counter())).get("validated")
        started = time.perf_counter()
        tasks = [asyncio.create_task(peak.request(client, rows[i], name, i, started+i*span/users)) for i in range(users)]
        samples = []
        async def monitor():
            while any(not task.done() for task in tasks):
                try:
                    samples.append({"elapsed_seconds": time.perf_counter()-started, **await snapshot(admin, stacks=len(samples)==2)})
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    samples.append({"monitor_unavailable": True})
                await asyncio.sleep(1)
        watcher = asyncio.create_task(monitor())
        results = await asyncio.gather(*tasks)
        elapsed = time.perf_counter()-started
        await watcher
    successes = [r for r in results if r.get("status")==200 and r.get("validated")]
    report = {"name": name, "virtual_users": users, "requests": len(results), "arrival_window_seconds": span,
              "offered_rps": users/span if span else None, "elapsed_seconds": elapsed,
              "successes": len(successes), "successful_rps": len(successes)/elapsed,
              "statuses": dict(Counter(str(r["status"]) for r in results if "status" in r)),
              "client_errors": dict(Counter(r["client_error"] for r in results if "client_error" in r)),
              "response_validation_failures": sum(r.get("status")==200 and not r.get("validated") for r in results),
              "latency_all_seconds": peak.percentiles([r["seconds"] for r in results]),
              "latency_success_seconds": peak.percentiles([r["seconds"] for r in successes]),
              "generator_schedule_lag_ms": peak.percentiles([r["schedule_lag_ms"] for r in results]),
              "samples": samples, "results": results}
    peak.save(name+".json", report)
    print(json.dumps({k:v for k,v in report.items() if k not in ("results","samples")}), flush=True)
    return report


async def main():
    assert not (peak.OUT/"baseline-summary.json").exists(), "Preserve completed control run"
    rows = await asyncio.to_thread(fixtures)
    reports = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5), trust_env=False) as admin:
        for name, users, span in (("baseline-paced-1000",1000,30),("baseline-paced-10000",10000,30),
                                  ("baseline-burst-1000",1000,0),("baseline-burst-10000",10000,0)):
            reports.append(await phase(name,users,span,rows,admin))
            peak.save("baseline-summary.json", [{k:v for k,v in r.items() if k not in ("results","samples")} for r in reports])


if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())

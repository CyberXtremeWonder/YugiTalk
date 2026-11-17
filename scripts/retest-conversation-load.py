"""在同一隔离数据集上复测限定消息扫描范围后的 API 负载。"""
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("improvement", ROOT / "scripts/verify-concurrency-improvement.py")
improvement = importlib.util.module_from_spec(spec)
spec.loader.exec_module(improvement)
baseline, peak = improvement.baseline, improvement.peak


async def main():
    """保留第一轮结果，另存源码指纹与四组相同负载。"""
    assert (peak.OUT / "summary.json").exists(), "Run initial isolated verification first"
    assert not (peak.OUT / "scoped-manifest.json").exists(), "Preserve completed retest"
    files = [ROOT/p for p in ("backend/app/chat/router.py", "backend/app/core/admission.py",
             "backend/app/core/database.py", "backend/app/identity/dependencies.py",
             "backend/app/tasks/notifications.py", "backend/app/tasks/router.py",
             "backend/app/main.py", "backend/app/core/config.py", "scripts/retest-conversation-load.py")]
    original = json.loads((peak.OUT/"manifest.json").read_text())
    original["scope"] = "Same API resources and workload; message aggregation restricted to current user"
    original["source_hashes"] = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    peak.save("scoped-manifest.json", original)
    rows = await asyncio.to_thread(baseline.fixtures)
    reports = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5), trust_env=False) as admin:
        for name,users,span in (("scoped-paced-1000",1000,30),("scoped-paced-10000",10000,30),
                                ("scoped-burst-1000",1000,0),("scoped-burst-10000",10000,0)):
            reports.append(await baseline.phase(name,users,span,rows,admin))
            peak.save("scoped-summary.json",[{k:v for k,v in r.items() if k not in ("results","samples")} for r in reports])


if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())

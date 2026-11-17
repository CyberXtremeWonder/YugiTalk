"""复用万人测试的客户端与隔离环境，验证三千用户均匀访问及突发。"""

import asyncio
import hashlib
import importlib.util
import json
import subprocess
import time
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/users-3000-20261108"
spec = importlib.util.spec_from_file_location(
    "improvement", ROOT / "scripts/verify-concurrency-improvement.py"
)
improvement = importlib.util.module_from_spec(spec)
spec.loader.exec_module(improvement)
baseline, peak = improvement.baseline, improvement.peak
peak.OUT = OUT


async def main():
    """另存本轮结果，不覆盖原始测试，不改变业务源码。"""
    assert not OUT.exists(), "Preserve existing test results"
    OUT.mkdir()
    previous = json.loads(
        (
            ROOT / "outputs/concurrency-improvement-20261108/capacity-manifest.json"
        ).read_text()
    )
    previous["users_per_phase"] = 3000
    previous["scope"] = (
        "80% authenticated conversation listing, 20% rename; one request per user; no RAG or generation"
    )
    previous["same_source_as_prior_capacity"] = all(
        hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == h
        for p, h in previous["source_hashes"].items()
    )
    previous["source_hashes"] = {
        p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
        for p in previous["source_hashes"]
    }
    previous["source_hashes"][str(Path(__file__).relative_to(ROOT))] = hashlib.sha256(
        Path(__file__).read_bytes()
    ).hexdigest()
    peak.save("manifest.json", previous)
    runtime = json.loads(
        (
            await asyncio.to_thread(
                subprocess.run,
                ["docker", "inspect", baseline.NAME],
                capture_output=True,
                text=True,
                check=True,
            )
        ).stdout
    )[0]
    assert runtime["HostConfig"]["NanoCpus"] == 4_000_000_000
    assert (
        runtime["Config"]["Cmd"][runtime["Config"]["Cmd"].index("--workers") + 1] == "4"
    )
    peak.save(
        "runtime.json",
        {
            "image_id": runtime["Image"],
            "command": runtime["Config"]["Cmd"],
            "nano_cpus": runtime["HostConfig"]["NanoCpus"],
            "memory_bytes": runtime["HostConfig"]["Memory"],
        },
    )
    # 原有隔离库中有一万名测试用户；这里只选用前三千人的新独立会话。
    rows = (await asyncio.to_thread(baseline.fixtures))[:3000]
    reports = []
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=5), trust_env=False
    ) as admin:
        for name, span in (("paced-3000", 30), ("burst-3000", 0)):
            report = await baseline.phase(name, 3000, span, rows, admin)
            assert report["requests"] == 3000
            assert len({r["user_index"] for r in report["results"]}) == 3000
            assert (
                sum(report["statuses"].values()) + sum(report["client_errors"].values())
                == 3000
            )
            assert report["response_validation_failures"] == 0
            reports.append(
                {k: v for k, v in report.items() if k not in ("results", "samples")}
            )
            peak.save("summary.json", reports)
        recovery = await peak.request(
            admin, rows[0], "recovery", 0, time.perf_counter(), "read"
        )
        assert recovery.get("validated"), recovery
        peak.save("recovery.json", recovery)
    assert all(
        hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == h
        for p, h in previous["source_hashes"].items()
    )
    peak.save(
        "verification.json",
        {
            "phases": 2,
            "requests": 6000,
            "distinct_users_each_phase": 3000,
            "response_validation_failures": 0,
            "source_hashes_unchanged": True,
            "post_burst_recovery_validated": True,
        },
    )


if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())

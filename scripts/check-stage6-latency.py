"""固定连接复用方式测试普通鉴权接口，不把健康探测代替业务性能。"""

import concurrent.futures
import json
import runpy
import ssl
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """在独立生产验收环境运行20并发，保留冷连接与预热后的两组原始耗时。"""
    values = dotenv_values(ROOT / ".cache/stage6-production.env", interpolate=False)
    output = ROOT / "docs/stages/artifacts/stage6-latency.json"
    if output.exists():
        output.with_name(f"stage6-latency-{time.time_ns()}.json").write_bytes(
            output.read_bytes()
        )
    helpers = runpy.run_path(str(ROOT / "scripts/check-stage6-production.py"))
    container = helpers["run"]([*helpers["compose"](), "ps", "-q", "api"]).strip()
    image, cpus = (
        helpers["run"](
            [
                "docker",
                "inspect",
                "--format",
                "{{.Image}} {{.HostConfig.NanoCpus}}",
                container,
            ]
        )
        .strip()
        .split()
    )
    context = ssl.create_default_context(
        cafile=str(ROOT / str(values["PROD_DATA_ROOT"]) / "tls/fullchain.pem")
    )
    with httpx.Client(
        base_url="https://127.0.0.1:18443",
        verify=context,
        trust_env=False,
        timeout=30,
        limits=httpx.Limits(max_connections=20),
    ) as client:
        identity = (
            client.post(
                "/api/v1/auth/login",
                headers={"Origin": str(values["PUBLIC_ORIGIN"])},
                json={
                    "email": "stage6@example.test",
                    "password": values["E2E_PASSWORD"],
                },
            )
            .raise_for_status()
            .json()
        )
        space = identity["user"]["id"]
        routes = [
            "/api/v1/auth/me",
            "/api/v1/auth/me",
            f"/api/v1/me/agents",
            f"/api/v1/me/conversations",
        ]

        def request(index: int) -> float:
            """检查真实业务状态码，测量到响应正文读取完毕的延迟。"""
            started = time.monotonic()
            client.get(routes[index % len(routes)]).raise_for_status()
            return time.monotonic() - started

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            cold = list(pool.map(request, range(40)))
            durations = list(pool.map(request, range(400)))
    report = {
        "concurrency": 20,
        "requests": 400,
        "errors": 0,
        "model_calls": 0,
        "api_image": image,
        "api_cpus": int(cpus) / 1e9,
        "routes": routes,
        "client": "直连IPv4；证书校验开启；40次连接预热",
        "cold_seconds": cold,
        "durations_seconds": durations,
        "p95_seconds": sorted(durations)[379],
    }
    report["status"] = "passed" if report["p95_seconds"] <= 0.5 else "failed"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("status", "requests", "concurrency", "p95_seconds")
            }
        )
    )
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()

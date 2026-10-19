"""以隔离数据和模型协议替身验收生产部署，不修改开发业务数据。"""

import argparse
import concurrent.futures
import json
import runpy
import secrets
import ssl
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".cache/stage6-production.env"
PROJECT = "yugitalk-stage6"


def run(arguments: list[str], input_text: str | None = None) -> str:
    """捕获命令输出，失败只输出服务诊断，不打印环境文件和解析配置。"""
    result = subprocess.run(
        arguments,
        input=input_text,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=1200,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr[-3000:] or "阶段6命令失败")
    return result.stdout


def compose(env_file: Path = ENV_FILE, project: str = PROJECT) -> list[str]:
    """固定三个合并文件，生产配置与测试替身的角色保持明确。"""
    return [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "-f",
        str(ROOT / "docker-compose.yml"),
        "-f",
        str(ROOT / "docker-compose.prod.yml"),
        "-f",
        str(ROOT / "docker-compose.stage6-test.yml"),
        "-p",
        project,
    ]


def prepare() -> dict[str, str]:
    """创建独立随机凭证和短期测试证书，只对首次验收初始化空环境。"""
    if ENV_FILE.exists():
        return {
            key: str(value)
            for key, value in dotenv_values(ENV_FILE).items()
            if value is not None
        }
    values = {
        name: secrets.token_hex(24)
        for name in (
            "POSTGRES_PASSWORD",
            "REDIS_PASSWORD",
            "STORAGE_ACCESS_KEY",
            "STORAGE_SECRET_KEY",
            "MILVUS_PASSWORD",
            "SESSION_SECRET",
            "METRICS_TOKEN",
            "EMBEDDING_API_KEY",
        )
    }
    development = dotenv_values(ROOT / ".env")
    values.update(
        {
            "MODEL_PROVIDER": "openai_compatible",
            "MODEL_ID": "stage6-protocol-fixture",
            "MODEL_BASE_URL": "https://model-fixture:8000/v1",
            "MODEL_API_KEY": secrets.token_hex(24),
            "BGE_MODEL_PATH": development.get("BGE_MODEL_PATH")
            or "./.cache/models/models--BAAI--bge-m3",
            "COMPOSE_PROFILES": "local-embedding",
            "PUBLIC_ORIGIN": "https://localhost:18443",
            "PROD_DATA_ROOT": "data/stage6-production",
            "STORAGE_ENDPOINT": "http://storage:9000",
            "STORAGE_BUCKET": "yugitalk",
            "STORAGE_REGION": "us-east-1",
            "S3_HOST": "storage",
            "S3_PORT": "9000",
            "S3_USE_SSL": "false",
            "MILVUS_STORAGE_BUCKET": "a-bucket",
            "MILVUS_STORAGE_PREFIX": "files",
            "HTTPS_BIND_ADDRESS": "127.0.0.1",
            "HTTPS_PORT": "18443",
            "HTTP_PORT": "18082",
            "OTEL_SAMPLE_RATIO": "1",
            "E2E_PASSWORD": secrets.token_urlsafe(20),
        }
    )
    ENV_FILE.parent.mkdir(exist_ok=True)
    initializer = runpy.run_path(str(ROOT / "scripts/init-production.py"))
    initializer["write_private"](
        ENV_FILE, "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"
    )
    initializer["initialize"](ENV_FILE, True)
    initializer["configure_permissions"](
        ROOT / values["PROD_DATA_ROOT"], "yugitalk-backend:0.2.0"
    )
    return values


def wait_ready(client: httpx.Client) -> None:
    """等待代理真正连接 API，就绪超时直接失败，不把静态页成功当作依赖正常。"""
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            if client.get("/health/ready").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError("生产隔离服务未能就绪")


def prepare_fixture_buckets(arguments: list[str]) -> None:
    """只在固定本机替身中建桶，模拟运维预置Bucket，不放宽正式应用权限。"""
    code = """
from app.core.config import Settings
from app.rag.storage import ObjectStorage
from botocore.exceptions import ClientError
s=Settings()
assert s.storage_endpoint=='http://storage:9000'
c=ObjectStorage(s).client
for bucket in (s.storage_bucket, 'a-bucket'):
 try:
  c.head_bucket(Bucket=bucket)
 except ClientError as error:
  if error.response['Error']['Code'] not in {'404','NoSuchBucket','NotFound'}:
   raise
  c.create_bucket(Bucket=bucket)
c.close()
"""
    run([*arguments, "exec", "-T", "api", "python", "-"], code)


def seed_and_check(
    client: httpx.Client, values: dict[str, str], email: str = "stage6@example.test"
) -> dict[str, object]:
    """在 HTTPS 下验证身份、安全头、流式任务和真实 BGE/Milvus 资料检索。"""
    origin = values["PUBLIC_ORIGIN"]
    response = client.post(
        "/api/v1/auth/login",
        headers={"Origin": origin},
        json={"email": email, "password": values["E2E_PASSWORD"]},
    )
    response.raise_for_status()
    assert (
        "Secure" in response.headers["set-cookie"]
        and "HttpOnly" in response.headers["set-cookie"]
    )
    identity = response.json()
    space = identity["user"]["id"]
    headers = {"Origin": origin, "X-CSRF-Token": identity["csrf_token"]}
    assert (
        client.post(
            "/api/v1/auth/logout", headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert client.get("/internal/metrics").status_code == 404
    assert client.get("/").headers["x-frame-options"] == "DENY"
    assert "max-age" in client.get("/").headers["strict-transport-security"]
    agent = (
        client.post(
            f"/api/v1/me/agents",
            headers=headers,
            json={"name": "生产隔离验收", "tools": [], "token_budget": 30000},
        )
        .raise_for_status()
        .json()
    )
    conversation = (
        client.post(
            f"/api/v1/me/conversations",
            headers=headers,
            json={"title": "HTTPS流式验收"},
        )
        .raise_for_status()
        .json()
    )
    task = (
        client.post(
            "/api/v1/runs",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "conversation_id": conversation["id"],
                "agent_version_id": agent["version_id"],
                "message": "请介绍这个隔离验收。",
            },
        )
        .raise_for_status()
        .json()
    )
    started = time.monotonic()
    text_times = []
    with client.stream("GET", f"/api/v1/runs/{task['id']}/events") as stream:
        stream.raise_for_status()
        assert stream.headers.get("x-accel-buffering") == "no"
        for line in stream.iter_lines():
            if line == "event: text_delta":
                text_times.append(time.monotonic() - started)
    terminal = client.get(f"/api/v1/runs/{task['id']}").raise_for_status().json()
    assert terminal["status"] == "succeeded", terminal.get("error_code")
    assert len(text_times) >= 3 and text_times[-1] - text_times[0] > 0.8
    base = (
        client.post(
            f"/api/v1/me/knowledge-bases",
            headers=headers,
            json={"name": "生产恢复资料"},
        )
        .raise_for_status()
        .json()["id"]
    )
    content = (
        "YugiTalk 阶段六恢复演练。验收口令为紫色星河。持久数据放在项目目录。".encode()
    )
    document = (
        client.post(
            f"/api/v1/knowledge-bases/{base}/documents",
            params={"filename": "recovery.txt"},
            headers={**headers, "Content-Type": "application/octet-stream"},
            content=content,
        )
        .raise_for_status()
        .json()
    )
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        documents = (
            client.get(f"/api/v1/knowledge-bases/{base}/documents")
            .raise_for_status()
            .json()["items"]
        )
        current = next(row for row in documents if row["id"] == document["id"])
        if current["status"] == "ready":
            break
        assert current["status"] != "failed", current.get("error_code")
        time.sleep(0.5)
    else:
        raise RuntimeError("真实 Embedding 导入超时")
    results = (
        client.post(
            f"/api/v1/knowledge-bases/{base}/search",
            headers=headers,
            json={"query": "验收口令是什么？"},
        )
        .raise_for_status()
        .json()
    )
    assert "紫色星河" in json.dumps(results, ensure_ascii=False)
    return {
        "email": email,
        "space": space,
        "base": base,
        "document": document["id"],
        "task": task["id"],
        "sse_text_times_seconds": text_times,
        "model": "协议替身，非真实模型",
        "real_bge_milvus": True,
    }


def check_monitoring(arguments: list[str], token: str) -> dict[str, object]:
    """通过内部网络验证指标认证、Prometheus 抓取和 Jaeger 实际接收追踪。"""
    code = """import httpx,json,os,time
from datetime import datetime,UTC,timedelta
with httpx.Client(trust_env=False,timeout=10) as c:
 assert c.get('http://127.0.0.1:8000/internal/metrics').status_code==401
 r=c.get('http://127.0.0.1:8000/internal/metrics',headers={'Authorization':'Bearer '+os.environ['METRICS_TOKEN']}); assert r.status_code==200
 assert 'yugitalk_runs' in r.text
 unauth=c.post('http://milvus:19530/v2/vectordb/collections/list',json={}); assert unauth.status_code!=200 or unauth.json().get('code')!=0
 end=time.monotonic()+60
 while time.monotonic()<end:
  services=c.get('http://jaeger:16686/api/v3/services').raise_for_status().json().get('services',[])
  metrics=c.get('http://prometheus:9090/api/v1/query',params={'query':'up{job="yugitalk-api"}'}).json()['data']['result']
  if 'yugitalk-api' in services and 'yugitalk-worker' in services and metrics and metrics[0]['value'][1]=='1': break
  time.sleep(1)
 else: raise AssertionError('监控未收到指标或追踪')
 now=datetime.now(UTC)
 params={'query.serviceName':'yugitalk-worker','query.startTimeMin':(now-timedelta(minutes=20)).isoformat(),'query.startTimeMax':now.isoformat(),'query.searchDepth':5}
 response=c.get('http://jaeger:16686/api/v3/traces',params=params).raise_for_status()
 traces=[json.loads(line)['result'] for line in response.text.splitlines() if line.strip()]
 assert traces
 assert any(attribute.get('key')=='service.name' and attribute.get('value',{}).get('stringValue')=='yugitalk-worker' for trace in traces for resource in trace.get('resourceSpans',[]) for attribute in resource.get('resource',{}).get('attributes',[]))
 print(json.dumps({'metrics_auth':True,'milvus_auth':True,'prometheus_up':True,'jaeger_api_worker':True,'worker_traces':len(traces),'jaeger_query_api':'v3'}))
"""
    return json.loads(run([*arguments, "exec", "-T", "api", "python", "-"], code))


def load_check(client: httpx.Client) -> dict[str, object]:
    """以有限并发测试健康接口延迟，明确不把它当作模型吞吐压测。"""

    def request(_: int) -> float:
        """计算单次 HTTPS 请求耗时，非成功状态立即失败。"""
        started = time.monotonic()
        client.get("/health/live").raise_for_status()
        return time.monotonic() - started

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        durations = sorted(executor.map(request, range(80)))
    return {
        "requests": 80,
        "concurrency": 8,
        "errors": 0,
        "p95_seconds": durations[75],
        "scope": "HTTPS存活接口；不代表真实模型吞吐",
    }


def main() -> None:
    """启动独立生产环境并保存可复核证据，失败时保留报告与诊断资源。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="保留隔离服务供人工验收")
    args = parser.parse_args()
    values = prepare()
    initializer = runpy.run_path(str(ROOT / "scripts/init-production.py"))
    fixture_tls = ROOT / values["PROD_DATA_ROOT"] / "fixture-tls"
    if not fixture_tls.exists():
        fixture_tls.mkdir()
        initializer["test_certificate"](fixture_tls)
    run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0",
            "--mount",
            f"type=bind,source={fixture_tls},target=/tls",
            "--entrypoint",
            "python",
            "yugitalk-backend:0.2.0",
            "-c",
            "import os; os.chown('/tls/privkey.pem',101,101); os.chmod('/tls/privkey.pem',0o600)",
        ]
    )
    arguments = compose()
    report: dict[str, object] = {"status": "running", "model_fixture": True}
    target = ROOT / "docs/stages/artifacts/stage6-production.json"
    try:
        config = json.loads(run([*arguments, "config", "--format", "json"]))
        for name, service in config["services"].items():
            if name != "frontend":
                assert not service.get("ports"), f"{name} 不得公开端口"
        run([*arguments, "up", "-d", "--wait", "--wait-timeout", "300"])
        prepare_fixture_buckets(arguments)
        # 重复验收保留已有身份，仅创建不存在的测试账号。
        check = run(
            [
                *arguments,
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "yugitalk",
                "-d",
                "yugitalk",
                "-At",
                "-c",
                "SELECT count(*) FROM users WHERE email='stage6@example.test';",
            ]
        )
        if check.strip() == "0":
            run(
                [
                    *arguments,
                    "exec",
                    "-T",
                    "api",
                    "python",
                    "-m",
                    "app.identity.bootstrap",
                    "--email",
                    "stage6@example.test",
                    "--name",
                    "阶段六验收",
                    "--password-stdin",
                ],
                values["E2E_PASSWORD"] + "\n",
            )
        context = ssl.create_default_context(
            cafile=str(ROOT / values["PROD_DATA_ROOT"] / "tls/fullchain.pem")
        )
        # 功能验收使用新身份，避免历史压测已消耗的额度污染重复验收；生产额度保持启用。
        email = "stage6-functional-" + uuid4().hex + "@example.test"
        run(
            [
                *arguments,
                "exec",
                "-T",
                "api",
                "python",
                "-m",
                "app.identity.bootstrap",
                "--email",
                email,
                "--name",
                "阶段六独立功能验收",
                "--password-stdin",
            ],
            values["E2E_PASSWORD"] + "\n",
        )
        with httpx.Client(
            base_url=values["PUBLIC_ORIGIN"],
            verify=context,
            trust_env=False,
            timeout=240,
        ) as client:
            wait_ready(client)
            report["functional"] = seed_and_check(client, values, email)
            report["load"] = load_check(client)
        report["monitoring"] = check_monitoring(arguments, values["METRICS_TOKEN"])
        report["status"] = "passed"
    except Exception as error:
        report["status"] = "failed"
        report["error_type"] = type(error).__name__
        raise
    finally:
        target.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if not args.keep:
            run([*arguments, "down"])
    print("阶段6 HTTPS、真实存储与监控验收通过；模型答复使用协议替身。")


if __name__ == "__main__":
    main()

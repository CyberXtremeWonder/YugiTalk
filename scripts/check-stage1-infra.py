"""验证真实开发容器；可选故障演练只操作固定的阶段 1 Compose 项目。"""

import argparse
import json
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = [
    "docker",
    "compose",
    "-f",
    str(ROOT / "docker-compose.yml"),
    "-p",
    "yugitalk-dev",
]


def run(*arguments: str, input_text: str | None = None) -> str:
    """执行有超时上限的验收命令，失败时终止后续检查。"""
    result = subprocess.run(
        [*COMPOSE, *arguments],
        input=input_text,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=90,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"容器命令失败：{arguments[0]}")
    return result.stdout


def health(path: str) -> tuple[int, dict[str, object]]:
    # 本机验收直连开发服务，不使用操作系统代理。
    """请求健康接口并校验服务就绪状态。"""
    opener = build_opener(ProxyHandler({}))
    try:
        response = opener.open(f"http://127.0.0.1:8080{path}", timeout=10)
    except HTTPError as error:
        response = error
    with response:
        # API 重启期间代理可能返回 HTML 502，作为未就绪处理并重试。
        try:
            body = json.load(response)
        except json.JSONDecodeError:
            body = {}
        return response.status, body


def wait_ready() -> None:
    """在限定时间内轮询服务就绪状态，超时即报告失败。"""
    for _ in range(30):
        try:
            status, body = health("/health/ready")
        except URLError:
            time.sleep(1)
            continue
        if status == 200 and body["status"] == "ready":
            return
        time.sleep(1)
    raise RuntimeError("依赖未在规定时间恢复")


def main() -> None:
    """运行本文件定义的验收步骤，并保存可复核的结果。"""
    parser = argparse.ArgumentParser(description="阶段 1 真实基础设施验收")
    parser.add_argument(
        "--fault", action="store_true", help="停止开发 Redis 验证故障后恢复"
    )
    args = parser.parse_args()
    wait_ready()
    assert health("/health/live")[0] == 200
    result = run(
        "exec",
        "-T",
        "api",
        "python",
        "-",
        input_text="from app.tasks.celery_app import celery_app\nresult = celery_app.send_task('yugitalk.infrastructure.ping')\nassert result.get(timeout=20) == {'status': 'ok'}\nprint('队列往返通过')\n",
    )
    assert "队列往返通过" in result
    checks = ["API 存活", "真实依赖就绪", "队列任务往返"]
    vector_result = run(
        "exec",
        "-T",
        "api",
        "python",
        "-",
        input_text=(ROOT / "backend/probes/milvus.py").read_text(encoding="utf-8"),
    )
    assert json.loads(vector_result)["status"] == "passed"
    checks.append("Milvus 集合创建、向量写入、检索与清理")
    if args.fault:
        try:
            run("stop", "redis")
            status, body = health("/health/ready")
            assert status == 503 and body["checks"]["redis"] is False
            assert health("/health/live")[0] == 200
            checks.append("Redis 故障：ready=503，live=200")
        finally:
            run("start", "redis")
        wait_ready()
        checks.append("Redis 恢复：ready=200")
        try:
            run("stop", "milvus")
            status, body = health("/health/ready")
            assert status == 503 and body["checks"]["milvus"] is False
            assert health("/health/live")[0] == 200
        finally:
            run("start", "milvus")
        # Milvus 重启需要加载索引，比 Redis 恢复耗时更长。
        wait_ready()
        checks.append("Milvus 故障与恢复")
        try:
            run("stop", "api")
            logs = run("logs", "--tail", "30", "api")
            assert "API 已完成关闭" in logs
        finally:
            run("start", "api")
        wait_ready()
        checks.append("API 优雅退出与重启")
        # 重建会更换容器身份，前端必须自行恢复连接，无需手动重启 Nginx。
        original_api = run("ps", "-q", "api").strip()
        run("up", "-d", "--no-deps", "--force-recreate", "api")
        assert run("ps", "-q", "api").strip() != original_api
        wait_ready()
        checks.append("API 容器重建后前端代理恢复")
    report = {"status": "passed", "checks": checks}
    (ROOT / "docs/stages/artifacts/stage1-infrastructure.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()

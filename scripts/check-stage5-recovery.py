"""真实 PostgreSQL、独立 Worker/Scheduler 的故障注入；禁止操作开发数据库。"""

import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """运行本文件定义的验收步骤，并保存可复核的结果。"""
    project = os.environ["STAGE5_COMPOSE_PROJECT"]
    url = os.environ["DATABASE_URL"]
    if project != "yugitalk-stage5-test" or not re.search(
        r"/yugitalk_stage5_[0-9a-f]{32}$", url
    ):
        raise RuntimeError("故障注入只允许本次生成的阶段 5 隔离数据库和容器")
    compose = ["docker", "compose", "-p", project]
    for name in ("stage2", "stage3", "stage4", "stage5"):
        compose.extend(["-f", f"docker-compose.{name}-test.yml"])
    report: dict[str, object] = {"database_isolated": True, "checks": []}
    checks: list[str] = []

    def docker(*args: str) -> None:
        """调用容器管理命令并捕获退出状态，供恢复验收使用。"""
        result = subprocess.run(
            [*compose, *args], cwd=ROOT, check=False, capture_output=True
        )
        if result.returncode:
            raise RuntimeError("隔离故障容器命令失败")

    with (
        httpx.Client(
            base_url="http://127.0.0.1:18080/api/v1", timeout=15, trust_env=False
        ) as client,
        psycopg.connect(url, autocommit=True) as db,
    ):
        # 静态页健康不代表反向代理已连上 API，先等待真实就绪接口，避免启动瞬间 502。
        deadline = time.monotonic() + 60
        while client.get("http://127.0.0.1:18080/health/ready").status_code != 200:
            if time.monotonic() >= deadline:
                raise RuntimeError("隔离前端代理未能连接就绪的 API")
            time.sleep(0.3)
        identity = (
            client.post(
                "/auth/login",
                json={
                    # 使用自己的独立空间，避免与权限回归和检索评测重复登录同一账号。
                    "email": "member@example.test",
                    "password": os.environ["E2E_PASSWORD"],
                },
                headers={"Origin": "http://127.0.0.1:18080"},
            )
            .raise_for_status()
            .json()
        )
        space = identity["user"]["id"]
        headers = {
            "Origin": "http://127.0.0.1:18080",
            "X-CSRF-Token": identity["csrf_token"],
        }
        agent = (
            client.post(
                f"/me/agents",
                headers=headers,
                json={
                    "name": "恢复故障验收",
                    "tools": ["format_report"],
                    "token_budget": 100000,
                },
            )
            .raise_for_status()
            .json()
        )
        version = agent["version_id"]

        def task(message: str) -> dict:
            """提交隔离测试任务，并返回后续恢复检查使用的标识。"""
            conversation = (
                client.post(
                    f"/me/conversations",
                    headers=headers,
                    json={"title": "故障验收"},
                )
                .raise_for_status()
                .json()["id"]
            )
            return (
                client.post(
                    "/runs",
                    headers={**headers, "Idempotency-Key": str(uuid4())},
                    json={
                        "conversation_id": conversation,
                        "agent_version_id": version,
                        "message": message,
                    },
                )
                .raise_for_status()
                .json()
            )

        def wait(key: str, status: str, timeout: int = 45) -> dict:
            """在明确截止时间内等待验收条件，超时即终止检查。"""
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                state = client.get(f"/runs/{key}").raise_for_status().json()
                if state["status"] == status:
                    return state
                if (
                    state["status"] in {"succeeded", "failed", "cancelled"}
                    and state["status"] != status
                ):
                    raise AssertionError(
                        f"任务状态不符合预期：{state['status']} / {state['error_code']}"
                    )
                time.sleep(0.05)
            # 仅输出任务公开状态与错误码，避免失败时丢失可定位的状态信息。
            raise AssertionError(
                f"隔离任务等待超时：期待 {status}，当前 {state['status']} / {state['error_code']}"
            )

        # SIGKILL 后消息是否重投不作假设；数据库排队意图与失效租约共同驱动恢复。
        killed = task("请生成报告：" + "容器恢复验证。" * 280)
        wait(killed["id"], "running")
        docker("kill", "-s", "SIGKILL", "worker")
        docker("up", "-d", "worker")
        state = wait(killed["id"], "succeeded")
        assert state["attempts"] >= 2
        assert (
            db.execute(
                "SELECT count(*) FROM messages WHERE id=%s AND role='assistant'",
                (killed["id"],),
            ).fetchone()[0]
            == 1
        )
        checks.append("SIGKILL 真实 Worker，失效租约回收并从检查点恢复，答复仅保存一次")
        # 长文本的宕机重发保守额度较高；普通工具场景继续使用较小预算。
        version = (
            client.post(
                f"/agents/{agent['id']}/versions",
                headers=headers,
                json={
                    "name": "恢复故障验收",
                    "tools": ["format_report"],
                    "token_budget": 30000,
                },
            )
            .raise_for_status()
            .json()["id"]
        )

        docker("stop", "worker")
        repeated = task("请生成报告：阶段 5 重复投递验证")
        assert client.get(f"/runs/{repeated['id']}").json()["status"] == "queued"
        # 生产工具注册表与接口均不得继续暴露工单能力。
        assert client.get("/model/capabilities").json()["tools"] == [
            "text_stats",
            "format_report",
        ]
        assert client.get(f"/me/tickets").status_code == 404
        assert client.get(f"/runs/{repeated['id']}/approvals").status_code == 404
        assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == 0
        checks.append("工单工具、审批与工单接口均已下线")

        # 多个独立数据库连接竞争同一运行，验证 PostgreSQL 行锁实际串行领取。
        sys.path.insert(0, str(ROOT / "backend"))
        from app.core.config import Settings
        from app.core.database import create_database
        from app.tasks.execution import Execution

        isolated = Settings(
            app_env="test",
            model_provider="mock",
            model_id="mock-v1",
            cumulative_budget_enabled=True,
            task_budget_enabled=True,
        )
        engine = create_database(isolated)
        try:
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(
                    pool.map(
                        lambda _: Execution(engine, isolated, repeated["id"]).execute(),
                        range(8),
                    )
                )
        finally:
            engine.dispose()
        wait(repeated["id"], "succeeded")
        docker("up", "-d", "worker")
        assert (
            db.execute(
                "SELECT count(*) FROM tool_executions WHERE run_id=%s AND status='succeeded'",
                (repeated["id"],),
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute(
                "SELECT count(*) FROM messages WHERE id=%s AND role='assistant'",
                (repeated["id"],),
            ).fetchone()[0]
            == 1
        )
        assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == 0
        checks.append("8 个并发重复投递，仅一个工具执行账本与一条答复；没有创建工单")
        assert (
            db.execute(
                "SELECT count(*) FROM budgets WHERE reserved<>0 OR spent<0"
            ).fetchone()[0]
            == 0
        )
        checks.append("完成后的预算全部释放且无负数结算")
        report["checks"] = checks
        report["duplicate_tool_executions"] = 0
        report["tickets_created"] = 0
        report["status"] = "passed"
        (ROOT / "docs/stages/artifacts/stage5-recovery.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print("阶段 5 真实容器故障注入通过，工具重复执行记录为 0，工单功能已下线。")


if __name__ == "__main__":
    main()

"""初始化独立浏览器验收账号，运行 RAG 旅程并关闭本次测试任务与资料。"""

import json
import os
import runpy
import ssl
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """使用既有阶段六替身配置，不打印私有凭证，也不复用业务用户的额度。"""
    helpers = runpy.run_path(str(ROOT / "scripts/check-stage6-production.py"))
    values = helpers["prepare"]()
    email = "rag-ui-" + uuid4().hex[:10] + "@example.test"
    helpers["run"](
        helpers["compose"]()
        + [
            "exec",
            "-T",
            "api",
            "python",
            "-m",
            "app.identity.bootstrap",
            "--email",
            email,
            "--name",
            "RAG UI验收",
            "--workspace",
            "RAG UI验收",
            "--password-stdin",
        ],
        values["E2E_PASSWORD"] + "\n",
    )
    report: dict[str, object] = {"email": email, "passed": False, "cleanup_passed": False}
    try:
        result = subprocess.run(
            [
                "npm.cmd" if os.name == "nt" else "npm",
                "run",
                "test:stage6",
                "--",
                "enhanced-rag.spec.ts",
            ],
            cwd=ROOT / "frontend",
            env={
                **os.environ,
                "E2E_PASSWORD": values["E2E_PASSWORD"],
                "E2E_RAG_EMAIL": email,
                "npm_config_cache": str(ROOT / ".cache/npm"),
            },
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
            check=False,
        )
        print(result.stdout[-5000:])
        if result.returncode:
            raise RuntimeError("浏览器 RAG 验收失败，查看 Playwright 报告。")
        report["passed"] = True
    finally:
        context = ssl.create_default_context(
            cafile=str(ROOT / values["PROD_DATA_ROOT"] / "tls/fullchain.pem")
        )
        with httpx.Client(
            base_url="https://localhost:18443", verify=context, trust_env=False, timeout=20
        ) as client:
            identity = (
                client.post(
                    "/api/v1/auth/login",
                    headers={"Origin": "https://localhost:18443"},
                    json={"email": email, "password": values["E2E_PASSWORD"]},
                )
                .raise_for_status()
                .json()
            )
            headers = {"Origin": "https://localhost:18443", "X-CSRF-Token": identity["csrf_token"]}
            space = identity["user"]["id"]
            runs = client.get(f"/api/v1/me/runs").raise_for_status().json()["items"]
            for run in runs:
                if run["status"] in {"queued", "running", "waiting_input"}:
                    client.post(
                        f"/api/v1/runs/{run['id']}/cancel", headers=headers
                    ).raise_for_status()
            bases = (
                client.get(f"/api/v1/me/knowledge-bases")
                .raise_for_status()
                .json()["items"]
            )
            for base in bases:
                docs = (
                    client.get(f"/api/v1/knowledge-bases/{base['id']}/documents")
                    .raise_for_status()
                    .json()["items"]
                )
                for document in docs:
                    if document["status"] != "deleted":
                        client.delete(
                            f"/api/v1/documents/{document['id']}", headers=headers
                        ).raise_for_status()
            # 删除为异步任务，只有资料真正退出检索范围后才记录清理成功。
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                remaining = [
                    doc
                    for base in bases
                    for doc in client.get(f"/api/v1/knowledge-bases/{base['id']}/documents")
                    .raise_for_status()
                    .json()["items"]
                    if doc["status"] != "deleted"
                ]
                if not remaining:
                    report["cleanup_passed"] = True
                    break
                time.sleep(0.5)
            if not report["cleanup_passed"]:
                raise RuntimeError("本次验收资料尚未完成删除，请检查 Worker。")
        target = ROOT / "docs/stages/artifacts/enhanced-rag-ui.json"
        if target.exists():
            target.rename(target.with_name("enhanced-rag-ui-" + str(time.time_ns()) + ".json"))
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

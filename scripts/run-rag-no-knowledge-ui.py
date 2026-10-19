"""使用专用开发验收身份验证空知识库拒答，不发送远端模型请求或操作用户资料。"""

import json
import os
import secrets
import subprocess
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """校验不会调用远端规划模型，再通过浏览器验收并保存独立报告。"""
    compose = ["docker", "compose", "exec", "-T", "api"]
    checked = subprocess.run(
        compose
        + [
            "python",
            "-c",
            "from app.core.config import load_settings; assert not load_settings().rag_fast_model",
        ],
        cwd=ROOT,
        capture_output=True,
        timeout=20,
        check=False,
    )
    if checked.returncode:
        raise RuntimeError("仅允许未配置远端规划模型的本机开发空库验收")
    email = "empty-rag-" + uuid4().hex[:10] + "@example.test"
    password = secrets.token_urlsafe(32)
    initialized = subprocess.run(
        compose
        + [
            "python",
            "-m",
            "app.identity.bootstrap",
            "--email",
            email,
            "--name",
            "空库验收",
            "--workspace",
            "空库验收",
            "--password-stdin",
        ],
        cwd=ROOT,
        input=password + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    if initialized.returncode:
        raise RuntimeError("无法初始化专用验收身份")
    result = subprocess.run(
        [
            "npm.cmd" if os.name == "nt" else "npm",
            "run",
            "test:stage6",
            "--",
            "rag-no-knowledge.spec.ts",
            "--reporter=list",
        ],
        cwd=ROOT / "frontend",
        env={
            **os.environ,
            "E2E_EMPTY_RAG_EMAIL": email,
            "E2E_EMPTY_RAG_PASSWORD": password,
            "npm_config_cache": str(ROOT / ".cache/npm"),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
        check=False,
    )
    report = {
        "passed": result.returncode == 0,
        "url": "http://127.0.0.1:8080",
        "expected_answer": "没有相关知识",
        "remote_model_calls": 0,
        "test_identity": email,
        "uploaded_documents": 0,
        "business_documents_touched": False,
    }
    (ROOT / "docs/stages/artifacts/rag-no-knowledge-ui.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(result.stdout[-3000:])
    if result.returncode:
        raise RuntimeError("空知识库浏览器验收失败，查看测试输出")


if __name__ == "__main__":
    main()

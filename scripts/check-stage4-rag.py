"""固定中文数据集的真实 BGE-M3 检索评测；只访问隔离验收服务。"""

import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DATASET = [
    (
        "向量存储.md",
        "YugiTalk 使用 Milvus 作为向量数据库，保存文档分块的 1024 维 BGE-M3 向量。PostgreSQL 保存用户、文档元数据和索引状态。",
    ),
    (
        "容器部署.md",
        "启动项目使用 docker compose up -d。API 和 Worker 复用同一个后端镜像，前端使用 Nginx 提供页面和同源 API 代理。",
    ),
    (
        "个人偏好.md",
        "长期记忆只保存用户主动确认的个人偏好。用户可以修改、删除或关闭记忆。关闭后不读取、不写入，也不向模型注入偏好。",
    ),
    (
        "文档格式.md",
        "知识库支持 UTF-8 TXT、Markdown 和可提取文本的 PDF，最大 20 MB。扫描 PDF 需要 OCR，本版暂不支持；加密 PDF 也不能导入。",
    ),
    (
        "数据删除.md",
        "删除文档时先在 PostgreSQL 写入删除状态，立即停止检索，再异步删除 Milvus 向量和 S3 原文。清理失败可以重试。",
    ),
]
CASES = [
    ("项目的向量数据保存在哪个数据库？", "向量存储.md"),
    ("BGE-M3 生成的向量是多少维？", "向量存储.md"),
    ("怎样通过容器启动项目？", "容器部署.md"),
    ("API 和 Worker 是否需要两个不同的镜像？", "容器部署.md"),
    ("不想让助手使用我的偏好，该如何操作？", "个人偏好.md"),
    ("个人偏好会被自动保存吗？", "个人偏好.md"),
    ("知识库能上传扫描版 PDF 吗？", "文档格式.md"),
    ("文件最大可以上传多少 MB？", "文档格式.md"),
    ("删除资料后什么时候停止检索？", "数据删除.md"),
    ("原文和向量清理失败以后怎么办？", "数据删除.md"),
]


def evaluate() -> None:
    """使用固定问题和实际嵌入检索评测命中表现，保存验收结果。"""
    report = {
        "embedding": "local BAAI/bge-m3",
        "dimension": 1024,
        "chat_model": "mock",
        "cases": [],
        "top1_accuracy": 0.0,
        "recall_at_5": 0.0,
    }
    with httpx.Client(
        base_url="http://127.0.0.1:18080", timeout=150, trust_env=False
    ) as client:
        response = client.post(
            "/api/v1/auth/login",
            headers={"Origin": "http://127.0.0.1:18080"},
            json={
                "email": "owner@example.test",
                "password": os.environ["E2E_PASSWORD"],
            },
        )
        response.raise_for_status()
        identity = response.json()
        # 前序权限回归会改变部分空间的角色，评测必须选取仍可管理的空间。
        space = next(
            item["id"]
            for item in identity["workspaces"]
            if item["role"] in {"owner", "admin"}
        )
        headers = {
            "Origin": "http://127.0.0.1:18080",
            "X-CSRF-Token": identity["csrf_token"],
        }
        result = client.post(
            f"/api/v1/me/knowledge-bases",
            headers=headers,
            json={"name": "固定中文检索评测"},
        )
        result.raise_for_status()
        base = result.json()["id"]
        for filename, content in DATASET:
            response = client.post(
                f"/api/v1/knowledge-bases/{base}/documents",
                params={"filename": filename},
                content=content.encode(),
                headers={**headers, "Content-Type": "application/octet-stream"},
            )
            response.raise_for_status()
        deadline = time.monotonic() + 120
        while True:
            rows = client.get(f"/api/v1/knowledge-bases/{base}/documents").json()[
                "items"
            ]
            if any(row["status"] == "failed" for row in rows):
                raise RuntimeError("固定评测文档索引失败")
            if len(rows) == len(DATASET) and all(
                row["status"] == "ready" for row in rows
            ):
                break
            if time.monotonic() > deadline:
                raise RuntimeError("固定评测文档索引超时")
            time.sleep(1)
        correct = recalled = 0
        cases = []
        for query, expected in CASES:
            started = time.monotonic()
            response = client.post(
                f"/api/v1/knowledge-bases/{base}/search",
                headers=headers,
                json={"query": query},
            )
            response.raise_for_status()
            sources = response.json()["items"]
            names = [source["filename"] for source in sources]
            correct += bool(names and names[0] == expected)
            recalled += expected in names
            for source in sources:
                assert (
                    client.get(f"/api/v1/sources/{source['chunk_id']}").status_code
                    == 200
                )
            cases.append(
                {
                    "query": query,
                    "expected": expected,
                    "retrieved": names,
                    "scores": [source["score"] for source in sources],
                    "latency_ms": round((time.monotonic() - started) * 1000),
                }
            )
        report.update(
            cases=cases,
            top1_accuracy=correct / len(CASES),
            recall_at_5=recalled / len(CASES),
        )
        stage = 5 if os.environ.get("STAGE5_COMPOSE_PROJECT") else 4
        (ROOT / f"docs/stages/artifacts/stage{stage}-rag-evaluation.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if report["top1_accuracy"] < 0.8 or report["recall_at_5"] < 0.9:
            raise RuntimeError("检索评测未达到 Top1 80%、Recall@5 90% 的首版门槛")
    print("真实 BGE-M3 固定中文检索评测通过。")


if __name__ == "__main__":
    evaluate()

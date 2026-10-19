"""为手动评测提供完整 RAG 的 HTTP 调用适配，不绕过用户授权与任务流程。"""

import os
from contextlib import contextmanager
import time
from uuid import uuid4

import httpx


def config(name: str) -> str:
    """读取必要的私有评测配置，缺失时拒绝发送请求。"""
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"缺少评测配置：{name}")
    return value


@contextmanager
def rag_session():
    """一次评测复用登录 Cookie 与 CSRF，结束时关闭连接。"""
    with httpx.Client(base_url=config("RAG_TEST_API_URL"), timeout=20, trust_env=False) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"email": config("RAG_TEST_ACCOUNT"), "password": config("RAG_TEST_PASSWORD")},
            headers={"Origin": config("RAG_TEST_ORIGIN")},
        )
        if response.status_code == 403:
            raise RuntimeError(
                "登录接口返回 403：请检查 RAG_TEST_ORIGIN 是否在后端 "
                "AUTH_TRUSTED_ORIGINS 中。当前 Origin：" + config("RAG_TEST_ORIGIN")
            )
        if response.status_code == 429:
            raise RuntimeError("登录限流：请等待限流窗口结束后重试，不要连续重跑评测。")
        response.raise_for_status()
        headers = {
            "Origin": config("RAG_TEST_ORIGIN"),
            "X-CSRF-Token": response.json()["csrf_token"],
        }
        yield lambda question: _call_rag(client, headers, question)


def call_rag(question: str) -> dict:
    """独立调用入口；批量评测应通过 rag_session 复用登录。"""
    with rag_session() as rag:
        return rag(question)


def _call_rag(client: httpx.Client, headers: dict, question: str) -> dict:
    """使用已登录客户端，为每题创建独立对话并等待结果。"""
    response = client.post(
        "/api/v1/me/conversations",
        json={"title": "RAG 评测"},
        headers=headers,
    )
    response.raise_for_status()
    conversation = response.json()["id"]
    response = client.post(
        "/api/v1/runs",
        json={
            "conversation_id": conversation,
            "agent_version_id": config("RAG_TEST_AGENT_VERSION_ID"),
            "message": question,
            "knowledge_base_id": config("RAG_TEST_KNOWLEDGE_BASE_ID"),
        },
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    response.raise_for_status()
    run_id = response.json()["id"]
    deadline = time.monotonic() + 360
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/runs/{run_id}")
        response.raise_for_status()
        run = response.json()
        if run["status"] == "succeeded":
            response = client.get(
                f"/api/v1/conversations/{conversation}/messages", params={"limit": 100}
            )
            response.raise_for_status()
            answers = [row for row in response.json()["items"] if row["role"] == "assistant"]
            if not answers:
                raise RuntimeError("任务完成但未保存答案")
            return {"answer": answers[-1]["content"], "sources": answers[-1]["sources"]}
        if run["status"] in {"failed", "cancelled", "waiting_input", "waiting_approval"}:
            raise RuntimeError(
                f"任务未完成：{run['status']} / {run.get('error_code')}；"
                f"run_id={run_id}；model={run.get('model')}；"
                f"checkpoint_node={run.get('checkpoint_node')}"
            )
        time.sleep(1)
    client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers).raise_for_status()
    raise TimeoutError("等待超时，已请求取消任务")

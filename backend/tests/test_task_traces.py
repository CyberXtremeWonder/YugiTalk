"""任务明细的完整性、真实用量、重试及权限回归。"""

import asyncio
import json
import time
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_agent_graph import ScriptedProvider
from test_agents import setup_task
from test_identity import login

from app.core.config import Settings
from app.core.entities import now
from app.memory.models import Memory
from app.observability.trace_models import TraceStep
from app.observability.traces import recording, trace_step
from app.providers.client import HTTPProvider, Reply, ToolCall
from app.rag.contracts import EvidenceGrade
from app.rag.model import StructuredModel
from app.tasks.execution import Execution, finish_run
from app.tasks.models import Run

pytest_plugins = ["test_identity"]


def detail(client, key):
    """读取管理员详情并校验成功。"""
    response = client.get(f"/api/v1/admin/traces/{key}")
    assert response.status_code == 200, response.text
    return response.json()


def test_task_records_memory_loops_tools_and_real_usage(client):
    """两轮模型和工具按任务关联；只记录记忆统计，不暴露正文或参数。"""
    _, _, run, _ = setup_task(client, "统计 private-prompt", token_budget=100000)
    with Session(client.app.state.database) as db:
        owner = db.get(Run, run["id"]).owner_id
        db.add(Memory(account_id=owner, user_id=owner, content="private-memory"))
        db.commit()
    provider = ScriptedProvider([
        Reply(calls=[ToolCall(id="one", name="text_stats", arguments={"text": "private-tool"})],
              input_tokens=100, output_tokens=20),
        Reply(input_tokens=150, output_tokens=30),
    ])
    Execution(client.app.state.database, client.app.state.settings, run["id"]).execute(
        lambda *_: provider
    )
    data = detail(client, run["id"])
    assert data["summary"] == {
        "recorded": True, "model_calls": 2, "input_tokens": 250, "output_tokens": 50,
        "unknown_usage_calls": 0, "unrecorded_attempts": 0,
        "loops": 2, "tool_calls": 1, "tool_reuses": 0,
        "memory_reads": 1, "memory_hits": 1, "retrievals": 0,
    }
    assert data["run"]["input_tokens"] == 250
    assert all(step["status"] == "succeeded" for step in data["steps"])
    assert all(step["duration_ms"] is not None for step in data["steps"])
    assert "private-" not in json.dumps(data)
    loop = next(step for step in data["steps"] if step["kind"] == "loop")
    assert any(step["kind"] == "llm" and step["parent_id"] == loop["id"] for step in data["steps"])


def test_unknown_usage_preserves_known_subtotal(client):
    """一个请求没有用量时，总用量未知但已知部分仍可查看。"""
    _, _, run, _ = setup_task(client, token_budget=100000)
    provider = ScriptedProvider([
        Reply(calls=[ToolCall(id="one", name="text_stats", arguments={"text": "x"})],
              input_tokens=25, output_tokens=3), Reply(),
    ])
    Execution(client.app.state.database, client.app.state.settings, run["id"]).execute(lambda *_: provider)
    data = detail(client, run["id"])
    assert data["summary"]["input_tokens"] == 25
    assert data["summary"]["unknown_usage_calls"] == 1
    assert data["run"]["input_tokens"] is None


def test_http_retries_record_each_request(client):
    """实际网络重试分别记录，拒绝响应的未知用量不能被当作零。"""
    _, _, run, _ = setup_task(client)
    attempts = []

    def response(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(503)
        return httpx.Response(200, text=(
            'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}],'
            '"usage":{"prompt_tokens":10,"completion_tokens":2}}\n\ndata: [DONE]\n\n'
        ))

    settings = Settings(_env_file=None, app_env="test", model_api_key="private-key",
                        model_base_url="http://model.test", model_max_retries=1)
    provider = HTTPProvider(settings, "openai_compatible", "test-model", httpx.MockTransport(response))
    with recording(client.app.state.database, run["id"], 1):
        events = list(provider.stream([{"role": "user", "content": "private-prompt"}], [], 100))
    assert events[-1].reply.input_tokens == 10
    data = detail(client, run["id"])
    assert data["summary"]["model_calls"] == 2
    assert data["summary"]["unknown_usage_calls"] == 1
    assert [step["details"]["retry"] for step in data["steps"]] == [0, 1]
    assert "private-" not in json.dumps(data)


def test_rag_usage_included_in_terminal_task(client, monkeypatch):
    """结构化 RAG 模型真实用量进入统一任务汇总。"""
    _, _, run, _ = setup_task(client)
    original = httpx.AsyncClient
    grade = {"relevance": "strong", "answerability": "sufficient", "ambiguity": "none", "route": "answer"}
    frame = "data: " + json.dumps({
        "choices": [{"delta": {"content": json.dumps(grade)}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 80, "completion_tokens": 12},
    }) + "\n\ndata: [DONE]\n\n"
    transport = httpx.MockTransport(lambda _: httpx.Response(200, text=frame))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    settings = Settings(_env_file=None, app_env="test", model_provider="openai_compatible",
                        model_id="grader", model_api_key="private-key", model_base_url="http://model.test")
    with recording(client.app.state.database, run["id"], 1):
        asyncio.run(StructuredModel(settings, lambda *_: None).call(
            EvidenceGrade, "grade", {"question": "private"}, time.monotonic() + 10
        ))
    finish_run(client.app.state.database, run["id"], "succeeded", None, "ok", 0, 0, 0)
    data = detail(client, run["id"])
    assert data["run"]["input_tokens"] == 80
    assert data["run"]["output_tokens"] == 12
    assert data["steps"][0]["details"]["purpose"] == "grade"


def test_live_step_and_interrupted_execution(client):
    """开始记录即时可见；失效租约的未结束步骤呈现为中断。"""
    _, _, run, _ = setup_task(client)
    engine = client.app.state.database
    with Session(engine) as db:
        row = db.get(Run, run["id"])
        row.status = "running"
        row.lease_until = now() + timedelta(minutes=1)
        row.attempts = 1
        db.commit()
    with recording(engine, run["id"], 1), trace_step("tool", "text_stats"):
        data = detail(client, run["id"])
        assert data["steps"][0]["status"] == "running"
        assert data["steps"][0]["duration_ms"] is None
        with Session(engine) as db:
            row = db.get(Run, run["id"])
            row.lease_until = now() - timedelta(seconds=1)
            db.commit()
        assert detail(client, run["id"])["steps"][0]["status"] == "interrupted"


def test_resume_of_pre_upgrade_task_is_marked_partial(client):
    """升级前的执行没有 Trace 时，不把升级后的部分用量伪装为总用量。"""
    _, _, run, _ = setup_task(client)
    with recording(client.app.state.database, run["id"], 2), trace_step("llm", "model") as step:
        step.usage(Reply(input_tokens=10, output_tokens=2))
    finish_run(client.app.state.database, run["id"], "succeeded", None, "ok", 0, 10, 2)
    data = detail(client, run["id"])
    assert data["summary"]["unrecorded_attempts"] == 1
    assert data["summary"]["input_tokens"] == 10
    assert data["run"]["input_tokens"] is None


def test_permissions_history_pagination_and_deletion(client):
    """管理员权限、历史空记录、分页和删除均沿用当前资源边界。"""
    headers, body, run, _ = setup_task(client)
    assert detail(client, run["id"])["summary"]["recorded"] is False
    Execution(client.app.state.database, client.app.state.settings, run["id"]).execute()
    page = client.get("/api/v1/admin/traces?limit=1").json()
    assert page["items"][0]["id"] == run["id"]
    assert client.get("/api/v1/admin/traces?limit=101").status_code == 422
    login(client, "member@example.test")
    assert client.get("/api/v1/admin/traces").status_code == 403
    assert client.get(f"/api/v1/admin/traces/{run['id']}").status_code == 403
    identity = login(client)
    headers["X-CSRF-Token"] = identity["csrf_token"]
    assert client.delete(f"/api/v1/conversations/{body['conversation_id']}", headers=headers).status_code == 204
    with Session(client.app.state.database) as db:
        assert not list(db.scalars(select(TraceStep).where(TraceStep.run_id == run["id"])))

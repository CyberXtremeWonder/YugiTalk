"""验证角色密钥选取、兼容回退与任务快照隔离。"""

import asyncio
import json
import time

import httpx
import pytest

from app.core.config import Settings
from app.rag.contracts import EvidenceGrade, QueryPlan, freeze_rag_config
from app.rag.model import StructuredModel


@pytest.mark.parametrize("purpose", ["plan", "rewrite", "graph", "grade"])
@pytest.mark.parametrize("dedicated", [True, False])
def test_structured_calls_select_role_key(monkeypatch, purpose, dedicated):
    """评判用Grader密钥，其余结构化阶段用Fast密钥，空值沿用主密钥。"""
    payload = (
        {
            "relevance": "strong",
            "answerability": "sufficient",
            "ambiguity": "none",
            "route": "answer",
        }
        if purpose == "grade"
        else {"complexity": "simple", "sub_questions": []}
    )
    expected = (
        ("grader-fixture" if purpose == "grade" else "fast-fixture")
        if dedicated
        else "main-fixture"
    )

    def respond(request):
        """检查实际发出的认证头，返回完整流式协议结果。"""
        assert request.headers["Authorization"] == "Bearer " + expected
        frame = {"choices": [{"delta": {"content": json.dumps(payload)}, "finish_reason": "stop"}]}
        return httpx.Response(200, text="data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n")

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.rag.model.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    settings = Settings(
        _env_file=None,
        app_env="test",
        model_provider="openai_compatible",
        model_base_url="https://fixture.test/v1",
        model_api_key="main-fixture",
        rag_fast_api_key="fast-fixture" if dedicated else "",
        rag_grader_api_key="grader-fixture" if dedicated else "",
    )
    schema = EvidenceGrade if purpose == "grade" else QueryPlan
    asyncio.run(
        StructuredModel(settings, lambda *args: None).call(
            schema, purpose, {}, time.monotonic() + 2
        )
    )


def test_role_keys_are_not_frozen_in_task_snapshot():
    """所有RAG凭证均不进入可持久化配置，普通模型配置仍正常冻结。"""
    settings = Settings(
        _env_file=None,
        rag_fast_model="fast-model",
        rag_grader_model="grader-model",
        rag_fast_api_key="fast-fixture",
        rag_grader_api_key="grader-fixture",
        rag_rerank_api_key="rerank-fixture",
    )
    snapshot = freeze_rag_config(settings)
    assert snapshot["rag_fast_model"] == "fast-model"
    assert snapshot["rag_grader_model"] == "grader-model"
    serialized = json.dumps(snapshot)
    for role in ("fast", "grader", "rerank"):
        assert f"rag_{role}_api_key" not in snapshot
        assert f"{role}-fixture" not in serialized

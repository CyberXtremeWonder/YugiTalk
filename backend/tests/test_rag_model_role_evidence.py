"""验证完整聊天测评按真实来源原文评分，避免引用元数据被误算为零召回。"""

import importlib.util
from pathlib import Path

import httpx
import pytest


def evaluator():
    """只加载受主入口保护的评测脚本，不触发网络请求。"""
    path = Path(__file__).resolve().parents[2] / "scripts/test-rag-model-roles.py"
    spec = importlib.util.spec_from_file_location("role_evaluation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("merged", [False, True])
def test_source_text_is_fetched_with_identity_and_scores_correctly(merged):
    """父块只在实际合并时取回；正文通过授权接口获取，才能命中完整引文。"""
    module = evaluator()
    source = {
        "chunk_id": "leaf",
        "document_id": "doc",
        "index_version": 2,
        "evidence_id": "evidence",
        "parent_id": "parent",
        "merged": merged,
        "truncated": False,
    }

    def respond(request):
        """核对版本、证据身份与父块参数，返回测试原文。"""
        assert request.url.path == "/api/v1/sources/leaf"
        assert request.url.params["index_version"] == "2"
        assert request.url.params["evidence_id"] == "evidence"
        assert (request.url.params.get("parent_id") == "parent") == merged
        return httpx.Response(200, json={"content": "这是真实来源原文"})

    row = {"run": {"status": "succeeded"}, "sources": [source]}
    case = {"question": "问题", "relevant": [{"document_id": "doc", "quote": "真实来源原文"}]}
    with httpx.Client(
        base_url="http://fixture.test", transport=httpx.MockTransport(respond)
    ) as client:
        module.score_answer(client, case, row)
    assert row["evidence_score"]["recall"] == 1
    assert "content" not in source


def test_unknown_truncated_length_is_not_scored_as_full_evidence():
    """缺少真实截断长度时清除旧分数，不能用完整来源虚增召回。"""
    module = evaluator()
    row = {
        "run": {"status": "succeeded"},
        "sources": [{"truncated": True}],
        "evidence_score": {"recall": 1},
    }
    with httpx.Client() as client:
        module.score_answer(client, {"question": "问题"}, row)
    assert "evidence_score" not in row
    assert row["evidence_score_error"] == "truncated_source_length_unavailable"

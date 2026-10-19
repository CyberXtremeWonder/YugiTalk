"""离线检验评测脚本的指标分母、证据匹配与接口失败处理。"""

import importlib.util
import json
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def evaluator():
    """按文件路径加载脚本，不连接服务也不读取私有配置。"""
    path = Path(__file__).resolve().parents[2] / "scripts/evaluate-knowledge-retrieval.py"
    spec = importlib.util.spec_from_file_location("retrieval_evaluator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_multiple_evidence_recall_and_complete(evaluator):
    """命中一个文件不能冒充完整两跳证据；重复来源不重复计算召回。"""
    case = {
        "relevant": [
            {"document_id": "a", "quote": "支付使用DB1"},
            {"document_id": "b", "quote": "订单依赖支付"},
        ]
    }
    sources = [{"document_id": "a", "content": "支付使用 DB1"}] * 3
    result = evaluator.score_case(case, sources, 5)
    assert result["recall"] == 0.5
    assert result["hit"] is True
    assert result["complete"] is False
    assert result["reciprocal_rank"] == 1
    sources.append({"document_id": "b", "content": "订单依赖支付"})
    result = evaluator.score_case(case, sources, 5)
    assert result["recall"] == 1 and result["complete"] is True
    assert evaluator.score_case(case, sources, 1)["recall"] == 0.5


def test_identity_quote_and_merged_child(evaluator):
    """同名其他文件不算命中指定文档；父块包含原叶子时仍可匹配分块标注。"""
    source = {
        "document_id": "actual",
        "filename": "same.md",
        "chunk_id": "leaf",
        "child_ids": ["leaf", "sibling"],
        "content": "包含正确证据",
    }
    assert not evaluator.evidence_matches({"document_id": "other", "filename": "same.md"}, [source])
    assert evaluator.evidence_matches({"chunk_id": "sibling", "quote": "正确证据"}, [source])
    assert not evaluator.evidence_matches({"document_id": "actual", "quote": "缺失证据"}, [source])


def test_unlabeled_is_not_recall_and_errors_are_separate(evaluator):
    """未标注的答案覆盖只记代理指标，失败题只计完成率。"""
    proxy = evaluator.score_case({"answer": "完整答案"}, [{"content": "这里有完整答案"}], 5)
    assert proxy["unlabeled"] and proxy["answer_verbatim_covered"]
    assert "recall" not in proxy
    labeled = evaluator.score_case({"relevant": [{"filename": "a"}]}, [{"filename": "a"}], 5)
    report = evaluator.aggregate(
        [{**proxy, "seconds": 1}, {**labeled, "seconds": 2}, {"error": "timeout", "seconds": 65}], 5
    )
    assert report["completion_rate"] == 2 / 3
    assert report["recall_at_k"] == 1 and report["labeled_positive"] == 1
    assert report["latency_p95_seconds"] == 2
    assert evaluator.aggregate([{**proxy, "seconds": 1}], 5)["recall_at_k"] is None


def test_negative_empty_and_fixed_precision_denominator(evaluator):
    """检索空结果负例不参与正例召回，Precision 固定以 K 为分母。"""
    negative = evaluator.score_case({"expected_no_results": True}, [], 5)
    assert negative["negative_empty"] and "recall" not in negative
    result = evaluator.score_case({"relevant": [{"filename": "a"}]}, [{"filename": "a"}], 5)
    assert result["precision"] == 0.2


@pytest.mark.parametrize("status", [401, 403, 404, 429])
def test_permanent_error_stops_remaining_queries(evaluator, status):
    """会话、权限、目标或限流出错后停止后续请求，不重复触发登录限流。"""
    requests = []

    def handler(request):
        """记录是否发生多余请求。"""
        requests.append(request)
        return httpx.Response(status, text="private-response")

    with httpx.Client(
        base_url="https://fixture.test", transport=httpx.MockTransport(handler)
    ) as client:
        rows = evaluator.run_cases(client, {}, "base", [{"question": "一"}, {"question": "二"}], 5)
    assert len(requests) == 1
    assert rows[0]["error"] == f"http_{status}"
    assert rows[1]["error"].startswith("not_executed_after_")
    assert "private-response" not in json.dumps(rows)


@pytest.mark.parametrize(
    "case",
    [
        {"question": "问题", "relevant": []},
        {"question": "问题", "relevant": [{"quote": "没有身份"}]},
        {"question": "问题", "relevant": [{"filename": "a"}, {"filename": "a"}]},
        {"question": "问题", "relevant": [{"filename": "a"}], "expected_no_results": True},
    ],
)
def test_invalid_labels_are_rejected(evaluator, tmp_path, case):
    """空标注、无身份、重复证据和正负标签矛盾不能静默影响分母。"""
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([case]), encoding="utf-8")
    with pytest.raises(ValueError):
        evaluator.load_cases(path)


def test_export_template_is_offline_and_preserves_existing_file(evaluator, tmp_path, monkeypatch):
    """模板导出不登录服务，已有人工标注不能被覆盖。"""
    source, target = tmp_path / "cases.csv", tmp_path / "labels.json"
    source.write_text("question,answer\n测试问题,参考答案\n", encoding="utf-8-sig")
    monkeypatch.setattr(
        "sys.argv", ["evaluate", "--cases", str(source), "--export-template", str(target)]
    )
    assert evaluator.main() == 0
    assert json.loads(target.read_text(encoding="utf-8"))["cases"][0]["question"] == "测试问题"
    with pytest.raises(ValueError, match="已存在"):
        evaluator.main()


def test_successful_api_case_preserves_gold_and_provenance(evaluator):
    """逐题报告同时保留标准证据、来源身份及评分，便于人工核对漏召回。"""
    case = {"question": "问题", "relevant": [{"document_id": "a", "quote": "关键事实"}]}
    with httpx.Client(
        base_url="https://fixture.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"items": [{"document_id": "a", "chunk_id": "chunk", "content": "关键事实"}]},
            )
        ),
    ) as client:
        row = evaluator.run_cases(client, {}, "base", [case], 5)[0]
    assert row["recall"] == 1 and row["relevant"] == case["relevant"]
    assert row["sources"][0]["preview"] == "关键事实"

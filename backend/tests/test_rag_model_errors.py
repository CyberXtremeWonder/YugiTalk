"""评分传输故障的分类、重试成本、截止时间与取消边界回归，不调用真实模型。"""

import asyncio
import json
import time

import httpx
import pytest

from app.core.config import Settings
from app.providers.client import ProviderFailure
from app.rag.contracts import EvidenceGrade, QueryPlan
from app.rag.model import StructuredModel
from app.tasks.execution import log_execution_failure


def grade_frame() -> str:
    """构造通过严格评分结构校验的协议响应。"""
    result = {
        "relevance": "strong",
        "answerability": "sufficient",
        "ambiguity": "none",
        "route": "answer",
    }
    return (
        "data: "
        + json.dumps(
            {"choices": [{"delta": {"content": json.dumps(result)}, "finish_reason": "stop"}]}
        )
        + "\n\ndata: [DONE]\n\n"
    )


def configured_model(monkeypatch, handler, updates, retries=1):
    """注入离线 HTTP 传输并保留真实流解析和预算检查回调。"""
    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.rag.model.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    settings = Settings(
        _env_file=None,
        app_env="test",
        model_provider="openai_compatible",
        model_base_url="https://fixture.test/v1",
        model_api_key="test-only",
        model_max_retries=retries,
    )
    return StructuredModel(settings, updates)


@pytest.mark.parametrize("failure", ["network", 429, 503])
def test_transient_retry_reserves_each_attempt(monkeypatch, failure):
    """临时故障最多重试一次且每次都预留成本，成功结果仍通过严格 Schema。"""
    requests, updates = [], []

    def handler(request):
        """第一次故障、第二次成功，记录真实传输尝试次数。"""
        requests.append(request)
        if len(requests) == 1:
            if failure == "network":
                raise httpx.ConnectError("private-error-content", request=request)
            return httpx.Response(failure)
        return httpx.Response(200, text=grade_frame())

    model = configured_model(monkeypatch, handler, lambda *args: updates.append(args))
    result = asyncio.run(model.call(EvidenceGrade, "grade", {}, time.monotonic() + 3))
    assert result.route == "answer" and len(requests) == 2
    costs = [row[2] for row in updates if row[2] > 0]
    assert len(costs) == 2 and costs[0] == costs[1]


@pytest.mark.parametrize(
    ("status", "frame", "expected"),
    [
        (401, "", "model_authentication"),
        (403, "", "model_authentication"),
        (400, "private-error-content", "model_request_rejected"),
        (200, "data: malformed-json\n\ndata: [DONE]\n\n", "rag_model_protocol"),
    ],
)
def test_permanent_failures_are_not_retried(monkeypatch, status, frame, expected):
    """鉴权、请求和传输协议错误均不重试或回显服务内容。"""
    requests = []

    def handler(request):
        """返回固定故障并记录是否发生了额外调用。"""
        requests.append(request)
        return httpx.Response(status, text=frame)

    model = configured_model(monkeypatch, handler, lambda *args: None)
    with pytest.raises(ProviderFailure) as raised:
        asyncio.run(model.call(EvidenceGrade, "grade", {}, time.monotonic() + 3))
    assert raised.value.code == expected and len(requests) == 1
    assert "private-error-content" not in str(raised.value)


def test_network_retry_exhaustion_is_classified(monkeypatch):
    """持续网络失败在设定次数后退出，不退化为泛化的执行异常。"""
    requests = []

    def handler(request):
        """模拟持续连接失败。"""
        requests.append(request)
        raise httpx.ReadTimeout("private-error-content", request=request)

    model = configured_model(monkeypatch, handler, lambda *args: None)
    with pytest.raises(ProviderFailure) as raised:
        asyncio.run(model.call(EvidenceGrade, "grade", {}, time.monotonic() + 3))
    assert raised.value.code == "model_timeout_or_network" and len(requests) == 2


def test_invalid_structure_logs_safe_diagnostics(monkeypatch, caplog):
    """字段错误记录阶段和错误类型，不回显无效内容。"""
    frame = grade_frame().replace("strong", "private-invalid-value")
    model = configured_model(
        monkeypatch, lambda request: httpx.Response(200, text=frame), lambda *args: None
    )
    with pytest.raises(ProviderFailure, match="rag_structured_output_invalid"):
        asyncio.run(model.call(EvidenceGrade, "grade", {}, time.monotonic() + 3))
    assert "purpose=grade" in caplog.text and "literal_error" in caplog.text
    assert "private-invalid-value" not in caplog.text


@pytest.mark.parametrize("invalid", ["literal", "json", "inconsistent"])
def test_structure_correction_succeeds_and_reserves_cost(monkeypatch, invalid):
    """枚举、JSON语法和跨字段约束失败均可有界纠正，保持严格校验。"""
    requests, updates = [], []
    bad = {
        "literal": grade_frame().replace("strong", "private-invalid-value"),
        "json": grade_frame().replace('\\"relevance\\"', '\\"broken'),
        "inconsistent": grade_frame().replace("strong", "weak"),
    }[invalid]

    def handler(request):
        """第一次返回无效内容，纠正时返回合法内容。"""
        requests.append(json.loads(request.content))
        return httpx.Response(200, text=bad if len(requests) == 1 else grade_frame())

    model = configured_model(monkeypatch, handler, lambda *args: updates.append(args), retries=0)
    result = asyncio.run(model.call(EvidenceGrade, "grade", {}, time.monotonic() + 3))
    assert result.route == "answer" and len(requests) == 2
    assert len([row for row in updates if row[2] > 0]) == 2
    feedback = requests[1]["messages"][-1]["content"]
    assert "校验错误" in feedback and "private-invalid-value" not in feedback


def test_structure_correction_exhaustion(monkeypatch):
    """持续违反跨字段约束时只纠正一次，不能绕过严格校验。"""
    requests = []

    def handler(request):
        """始终声称弱证据足以直接回答。"""
        requests.append(request)
        return httpx.Response(200, text=grade_frame().replace("strong", "weak"))

    model = configured_model(monkeypatch, handler, lambda *args: None, retries=3)
    with pytest.raises(ProviderFailure, match="rag_structured_output_invalid"):
        asyncio.run(model.call(EvidenceGrade, "grade", {}, time.monotonic() + 3))
    assert len(requests) == 2


def test_plan_correction_preserves_business_constraints(monkeypatch):
    """复杂规划缺少子问题时重新生成，不能只靠合法JSON绕过业务约束。"""
    requests = []

    def handler(request):
        """首轮空规划，纠正后提供两个独立问题。"""
        requests.append(json.loads(request.content))
        payload = {
            "complexity": "complex",
            "sub_questions": [] if len(requests) == 1 else ["问题一", "问题二"],
        }
        frame = {"choices": [{"delta": {"content": json.dumps(payload)}, "finish_reason": "stop"}]}
        return httpx.Response(200, text="data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n")

    model = configured_model(monkeypatch, handler, lambda *args: None, retries=0)
    result = asyncio.run(model.call(QueryPlan, "plan", {}, time.monotonic() + 3))
    assert result.sub_questions == ["问题一", "问题二"] and len(requests) == 2
    assert "value_error" in requests[1]["messages"][-1]["content"]


def test_structure_correction_shares_deadline(monkeypatch):
    """纠正请求沿用原始截止时间，不能重置模型调用超时。"""
    requests = []

    async def handler(request):
        """首次快速返回无效结构，纠正响应超过剩余时间。"""
        requests.append(request)
        if len(requests) == 2:
            await asyncio.sleep(1)
        return httpx.Response(200, text=grade_frame().replace("strong", "weak"))

    model = configured_model(monkeypatch, handler, lambda *args: None)
    start = time.monotonic()
    with pytest.raises(ProviderFailure, match="rag_deadline"):
        asyncio.run(model.call(EvidenceGrade, "grade", {}, start + 0.2))
    assert len(requests) == 2 and time.monotonic() - start < 0.8


@pytest.mark.parametrize("failure", ["cancelled", "budget_exceeded"])
def test_structure_correction_obeys_guard(monkeypatch, failure):
    """纠正前再次检查取消和预算边界，失败时不得继续调用。"""
    requests = []
    reservations = []

    def handler(request):
        """返回合法协议中的非法评分。"""
        requests.append(request)
        return httpx.Response(200, text=grade_frame().replace("strong", "weak"))

    def update(stage, details, reserve):
        """第二次预算预留时阻止请求。"""
        if reserve:
            reservations.append(reserve)
            if len(reservations) == 2:
                raise ProviderFailure(failure)

    model = configured_model(monkeypatch, handler, update)
    with pytest.raises(ProviderFailure, match=failure):
        asyncio.run(model.call(EvidenceGrade, "grade", {}, time.monotonic() + 3))
    assert len(requests) == 1


@pytest.mark.parametrize("model_name", ["deepseek-flash", "other-model"])
def test_deepseek_structured_request_uses_json_without_thinking(monkeypatch, model_name):
    """DeepSeek 的结构化调用显式选择 JSON 和非思考模式，其他模型保持原协议。"""
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, text=grade_frame())

    model = configured_model(monkeypatch, handler, lambda *args: None)
    model.settings.model_id = model_name
    result = asyncio.run(model.call(EvidenceGrade, "grade", {}, time.monotonic() + 3))
    assert result.route == "answer"
    if model_name == "deepseek-flash":
        assert bodies[0]["thinking"] == {"type": "disabled"}
        assert bodies[0]["response_format"] == {"type": "json_object"}
    else:
        assert "thinking" not in bodies[0] and "response_format" not in bodies[0]


@pytest.mark.parametrize("stream_limit", [262_144, 4_194_304])
def test_structured_stream_uses_configured_protocol_capacity(monkeypatch, stream_limit):
    """小 JSON 的协议流可以超过 256 KiB，但仍遵循配置的容量限制。"""
    metadata = (
        "data: "
        + json.dumps({"choices": [{"delta": {"reasoning_content": "fixture" * 500}}]})
        + "\n\n"
    )
    frame = metadata * 100 + grade_frame()
    assert len(frame.encode()) > 262_144
    model = configured_model(
        monkeypatch, lambda request: httpx.Response(200, text=frame), lambda *args: None
    )
    model.settings.model_max_stream_bytes = stream_limit
    if stream_limit == 262_144:
        with pytest.raises(ProviderFailure, match="model_stream_size_limit"):
            asyncio.run(model.call(EvidenceGrade, "grade", {}, time.monotonic() + 3))
    else:
        result = asyncio.run(model.call(EvidenceGrade, "grade", {}, time.monotonic() + 3))
        assert result.route == "answer"


def test_structured_stream_preserves_text_size_limit(monkeypatch):
    """统一协议流容量后，正文超限仍拒绝处理。"""
    frame = (
        "\n\n".join(
            "data: " + json.dumps({"choices": [{"delta": {"content": "x" * 1000}}]})
            for _ in range(66)
        )
        + "\n\ndata: [DONE]\n\n"
    )
    model = configured_model(
        monkeypatch, lambda request: httpx.Response(200, text=frame), lambda *args: None
    )
    with pytest.raises(ProviderFailure, match="model_response_too_large"):
        asyncio.run(model.call(EvidenceGrade, "grade", {}, time.monotonic() + 3))


def test_retry_shares_absolute_deadline(monkeypatch):
    """退避等待计入总截止时间，不能为每次重试重置超时。"""
    requests = []

    def handler(request):
        """服务繁忙时必须由全局截止时间阻止第二次请求。"""
        requests.append(request)
        return httpx.Response(503)

    model = configured_model(monkeypatch, handler, lambda *args: None, retries=3)
    start = time.monotonic()
    with pytest.raises(ProviderFailure) as raised:
        asyncio.run(model.call(EvidenceGrade, "grade", {}, start + 0.05))
    assert raised.value.code == "rag_deadline" and len(requests) == 1
    assert time.monotonic() - start < 1


def test_cancel_during_backoff_prevents_retry(monkeypatch):
    """等待重试期间取消任务仍立即生效，不发送下一次请求。"""
    requests = []

    def handler(request):
        """制造临时故障以进入重试等待。"""
        requests.append(request)
        return httpx.Response(503)

    def update(*args):
        """首个请求后模拟权限边界回调发现任务取消。"""
        if requests:
            raise ProviderFailure("cancelled")

    model = configured_model(monkeypatch, handler, update)
    with pytest.raises(ProviderFailure) as raised:
        asyncio.run(model.call(EvidenceGrade, "grade", {}, time.monotonic() + 3))
    assert raised.value.code == "cancelled" and len(requests) == 1


def test_safe_execution_log_excludes_exception_contents(caplog):
    """未知异常只记录类型和定位信息，不记录可能包含用户资料的异常正文。"""
    try:
        raise ValueError("private-user-evidence-and-key")
    except ValueError as error:
        log_execution_failure("test-run-id", error)
    assert "test-run-id" in caplog.text and "ValueError" in caplog.text
    assert "test_rag_model_errors.py" in caplog.text
    assert "private-user-evidence-and-key" not in caplog.text

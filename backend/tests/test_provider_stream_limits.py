"""模型流的协议容量和正文容量分开验证，所有请求均为本地协议替身。"""

import json
from collections.abc import Iterator

import httpx
import pytest
from app.core.config import Settings
from app.providers.client import HTTPProvider, ProviderFailure
from app.tasks.execution import Execution
from pydantic import SecretStr
from test_agents import setup_task

pytest_plugins = ["test_identity"]


def frame(payload: dict) -> str:
    """生成离线 SSE 数据帧，保留中文真实 UTF-8 字节长度。"""
    return "data: " + json.dumps(payload, ensure_ascii=False)


def fragmented(protocol: str) -> Iterator[str]:
    """构造超过旧协议容量的内部推理与元数据，正文仅返回一句中文。"""
    for _ in range(400):
        if protocol == "openai_compatible":
            yield frame({"choices": [{"delta": {"reasoning_content": "内部推理" * 100}}]})
        else:
            yield frame(
                {
                    "type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "thinking": "内部推理" * 100},
                }
            )
    if protocol == "openai_compatible":
        yield frame(
            {
                "choices": [{"delta": {"content": "答案完整。"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 8},
            }
        )
        yield "data: [DONE]"
    else:
        yield frame({"type": "message_start", "message": {"usage": {"input_tokens": 10}}})
        yield frame(
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "答案完整。"}}
        )
        yield frame(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 8},
            }
        )
        yield frame({"type": "message_stop"})


@pytest.mark.parametrize("protocol", ["openai_compatible", "anthropic"])
def test_fragmented_protocol_does_not_truncate_small_answer(protocol: str) -> None:
    """默认容量接受碎片较多的合法流，内部推理不展示，最终用量正确保留。"""
    lines = list(fragmented(protocol))
    assert sum(len(line.encode()) for line in lines) > 262_144
    adapter = HTTPProvider(Settings(app_env="test"), protocol, "fixture")
    events = list(adapter.decode(iter(lines), 64))
    assert "".join(event.text for event in events) == "答案完整。"
    assert events[-1].reply.input_tokens == 10
    assert events[-1].reply.output_tokens == 8


@pytest.mark.parametrize("protocol", ["openai_compatible", "anthropic"])
def test_stream_capacity_is_configurable_and_still_bounded(protocol: str) -> None:
    """显式设置较小容量时拒绝超限协议流，未取消大小保护。"""
    adapter = HTTPProvider(
        Settings(app_env="test", model_max_stream_bytes=262_144), protocol, "fixture"
    )
    with pytest.raises(ProviderFailure, match="model_stream_size_limit"):
        list(adapter.decode(fragmented(protocol), 64))


def test_frame_limit_counts_utf8_bytes() -> None:
    """单帧按字节而非字符限制，避免多字节中文绕过保护。"""
    adapter = HTTPProvider(Settings(app_env="test"), "openai_compatible", "fixture")
    line = frame({"choices": [{"delta": {"content": "中" * 22_000}}]})
    assert len(line) < 65_536 and len(line.encode()) > 65_536
    with pytest.raises(ProviderFailure, match="model_stream_size_limit"):
        list(adapter.decode(iter([line]), 64))


@pytest.mark.parametrize("payload", ["text", "tool"])
def test_small_frames_cannot_bypass_payload_limit(payload: str) -> None:
    """小帧的正文或工具参数累计超限时仍终止，不用整体容量替代业务输入边界。"""
    adapter = HTTPProvider(Settings(app_env="test"), "openai_compatible", "fixture")
    delta = (
        {"content": "字" * 1000}
        if payload == "text"
        else {"tool_calls": [{"index": 0, "function": {"arguments": "字" * 1000}}]}
    )
    lines = (frame({"choices": [{"delta": delta}]}) for _ in range(23))
    with pytest.raises(ProviderFailure, match="model_response_too_large"):
        list(adapter.decode(lines, 4096))


def test_fragmented_stream_finishes_task_and_saves_usage(client) -> None:
    """经真实任务执行器验证碎片流保存完整答案与用量，HTTP 请求完全在内存拦截。"""
    settings = client.app.state.settings
    settings.model_provider = "openai_compatible"
    settings.model_id = "stream-fixture"
    settings.model_api_key = SecretStr("offline-test-key")
    settings.model_base_url = "https://stream.example.test"
    _, body, run, _ = setup_task(client)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="\n\n".join(fragmented("openai_compatible")))
    )

    def offline_provider(config, protocol, model):
        """注入本地协议替身，禁止向真实供应商发送问题或证据。"""
        return HTTPProvider(config, protocol, model, transport)

    Execution(client.app.state.database, settings, run["id"]).execute(offline_provider)
    result = client.get(f"/api/v1/runs/{run['id']}").json()
    assert result["status"] == "succeeded" and result["error_code"] is None
    assert result["input_tokens"] == 10 and result["output_tokens"] == 8
    messages = client.get(f"/api/v1/conversations/{body['conversation_id']}/messages").json()[
        "items"
    ]
    assert messages[-1]["content"] == "答案完整。"
    assert "内部推理" not in messages[-1]["content"]

"""工具白名单、参数修复与图循环上限回归，不连接外部模型。"""

from collections.abc import Callable, Iterator
from typing import Any

import pytest

from app.agents.contracts import AgentConfig
from app.agents.graph import run_graph
from app.core.config import Settings
from app.providers.client import ModelEvent, ProviderFailure, Reply, ToolCall


class ScriptedProvider:
    """按预设脚本返回模型回复，验证工具循环与检查点行为。"""

    def __init__(self, replies: list[Reply]) -> None:
        """保存实例所需的依赖与配置，初始化后续执行使用的状态。"""
        self.replies = iter(replies)
        self.guard: Callable[[], None] = lambda: None
        self.calls = 0

    def stream(
        self, messages: list[dict[str, Any]], tools: list[str], max_tokens: int
    ) -> Iterator[ModelEvent]:
        """逐步产生公开文本和结构化回复，并在调用期间检查取消与执行边界。"""
        self.guard()
        self.calls += 1
        reply = next(self.replies)
        if not reply.calls:
            yield ModelEvent("text", text="已完成")
        yield ModelEvent("reply", reply=reply)


def call(name: str = "text_stats", **arguments: object) -> Reply:
    """生成受控工具提案，供执行图测试复用。"""
    return Reply(calls=[ToolCall(id="call", name=name, arguments=arguments)])


def execute(provider: ScriptedProvider, **config: object):
    """领取排队任务，隔离重复领取并为执行期间维护租约。"""
    events = []

    def update(kind, payload, reserve):
        """续租并校验预算和来源，在同一事务中保存公开执行事件。"""
        if kind:
            events.append((kind, payload))

    result = run_graph(
        Settings(app_env="test"),
        AgentConfig(name="测试", **config),
        provider,
        [{"role": "user", "content": "测试"}],
        update,
        lambda text: None,
        0,
    )
    return result, events


def test_invalid_tool_arguments_can_be_repaired() -> None:
    """验证无效工具参数反馈到模型后允许在边界内修复。"""
    result, events = execute(
        ScriptedProvider([call(unexpected="输入"), call(text="你好"), Reply()])
    )
    assert result["step"] == 3 and result["tool_count"] == 2
    assert any(payload.get("error_code") == "tool_arguments_invalid" for _, payload in events)
    assert result["text"] == "已完成"


@pytest.mark.parametrize(
    "replies,config,error",
    [
        ([call("unknown", text="你好")], {}, "tool_not_authorized"),
        ([call(text="你好")], {"tools": []}, "tool_not_authorized"),
        ([call(text="你好")] * 3, {}, "no_progress"),
        ([call(text="你好")] * 2, {"max_steps": 1}, "model_step_limit"),
    ],
)
def test_execution_limits(replies, config, error) -> None:
    """验证模型轮次、工具次数与输出等执行上限生效。"""
    with pytest.raises(ProviderFailure, match=error):
        execute(ScriptedProvider(replies), **config)


def test_token_usage_aggregates_when_available() -> None:
    """验证供应商提供用量时按模型调用正确累加。"""
    first = call(text="你好")
    first.input_tokens, first.output_tokens = 10, 2
    result, _ = execute(ScriptedProvider([first, Reply(input_tokens=20, output_tokens=3)]))
    assert result["input_tokens"] == 30 and result["output_tokens"] == 5

"""以应用契约封装 LangGraph 模型／工具节点，不将图状态或推理链暴露给客户端。"""

import json
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any, NotRequired, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from app.agents.contracts import AgentConfig
from app.core.config import Settings
from app.guardrails.prompts import protect_messages
from app.observability.telemetry import operation_span
from app.observability.traces import Step, trace_step
from app.providers.client import HTTPProvider, Provider, ProviderFailure, Reply, ToolCall
from app.tools.registry import TOOL_SCHEMAS
from app.tools.runner import execute_tool

Update = Callable[[str | None, dict[str, object] | None, int], None]


class AgentState(TypedDict):
    """保存消息、工具游标、用量与停止标记，供执行图检查点恢复。"""

    messages: list[dict[str, Any]]
    text: str
    input_tokens: int | None
    output_tokens: int | None
    calls: list[ToolCall]
    step: int
    tool_count: int
    repetitions: dict[str, int]
    tool_cursor: int
    loop: NotRequired[dict[str, Any]]


def run_graph(
    settings: Settings,
    config: AgentConfig,
    provider: Provider,
    messages: list[dict[str, Any]],
    update: Update,
    on_text: Callable[[str], None],
    retries: int,
    resume: AgentState | None = None,
    node: str = "model",
    save: Callable[[AgentState, str], None] | None = None,
    tool_executor: Callable[[ToolCall, int], dict[str, object]] | None = None,
    _control: str | None = None,
    _control_prompt: str = "",
) -> AgentState:
    """两个节点形成有界循环；数据库负责取消、权限、事件和预算事实。"""

    if config.closed_loop and _control is None:
        from app.agents.loop import run_closed_loop

        return run_closed_loop(
            settings, config, provider, messages, update, on_text, retries,
            resume, node, save, tool_executor,
        )

    def model(state: AgentState) -> AgentState:
        """检查执行边界后调用模型，并累计输出与工具提案。"""
        if save:
            save(state, "model")
        if state["step"] >= min(config.max_steps, settings.max_model_steps):
            raise ProviderFailure("model_step_limit")
        # 输入字节数与工具 Schema 作为保守预留，不作为实际供应商 Token 用量。
        schemas = getattr(provider, "tool_schemas", TOOL_SCHEMAS)
        context_size = len(
            json.dumps(
                {"messages": state["messages"], "tools": [schemas[name] for name in config.tools]},
                ensure_ascii=False,
            ).encode("utf-8")
        )
        if context_size > 32768:
            raise ProviderFailure("context_limit")
        charge = (context_size + config.max_output_tokens + 2048) * (retries + 1)
        update("tool_status", {"stage": "model", "step": state["step"] + 1}, charge)
        provider.guard = lambda: update(None, None, 0)
        reply: Reply | None = None
        step_text = ""
        with operation_span("model.generate"), trace_step(
            "loop", "模型循环", iteration=state["step"] + 1
        ):
            with (
                nullcontext(Step()) if isinstance(provider, HTTPProvider) else trace_step(
                    "llm", getattr(provider, "model", settings.model_id),
                    provider=getattr(provider, "provider", settings.model_provider),
                )
            ) as request_step:
                for event in provider.stream(
                    state["messages"], list(config.tools), config.max_output_tokens
                ):
                    if event.kind == "text":
                        if (
                            not isinstance(event.text, str)
                            or len((state["text"] + step_text + event.text).encode()) > 65536
                        ):
                            raise ProviderFailure("model_response_too_large")
                        if not config.closed_loop:
                            update("text_delta", {"text": event.text}, 0)
                            on_text(event.text)
                        step_text += event.text
                    else:
                        reply = event.reply
                        update(None, None, 0)
                if reply is not None and not isinstance(provider, HTTPProvider):
                    request_step.usage(reply)
        if reply is None:
            raise ProviderFailure("model_protocol")
        if not reply.calls and not step_text.strip():
            raise ProviderFailure("model_empty_result")
        calls = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in reply.calls
        ]
        message: dict[str, Any] = {"role": "assistant", "content": step_text}
        if calls:
            message["tool_calls"] = calls
        result: AgentState = {
            **state,
            "step": state["step"] + 1,
            "text": (step_text if not reply.calls else "")
            if config.closed_loop else state["text"] + step_text,
            "messages": [*state["messages"], message],
            "calls": reply.calls,
            "tool_cursor": 0,
            "input_tokens": None
            if state["input_tokens"] is None or reply.input_tokens is None
            else state["input_tokens"] + reply.input_tokens,
            "output_tokens": None
            if state["output_tokens"] is None or reply.output_tokens is None
            else state["output_tokens"] + reply.output_tokens,
        }
        if save:
            save(result, "tools" if reply.calls else "done")
        return result

    def tools(state: AgentState) -> AgentState:
        """按游标校验并执行已许可工具，逐项保存恢复检查点。"""
        result_state: AgentState = {
            **state,
            "messages": list(state["messages"]),
            "repetitions": dict(state["repetitions"]),
        }
        for cursor, call in enumerate(state["calls"]):
            if cursor < state["tool_cursor"]:
                continue
            update(None, None, 0)
            if call.name not in config.tools:
                raise ProviderFailure("tool_not_authorized")
            if result_state["tool_count"] + 1 > min(config.max_tool_calls, settings.max_tool_calls):
                raise ProviderFailure("tool_call_limit")
            fingerprint = json.dumps(
                {"name": call.name, "arguments": call.arguments}, sort_keys=True, ensure_ascii=False
            )
            count = result_state["repetitions"].get(fingerprint, 0) + 1
            if count > 2:
                raise ProviderFailure("no_progress")
            try:
                with operation_span("tool." + call.name), trace_step(
                    "tool", call.name, iteration=state["step"], call_id=call.id
                ) as tool_step:
                    result = (
                        tool_executor(call, state["step"])
                        if tool_executor
                        else execute_tool(call.name, call.arguments)
                    )
                    tool_step.status = "failed" if result.get("ok") is False else "succeeded"
                    if result.get("ok") is False:
                        tool_step.details["error_code"] = str(
                            result.get("error_code", "tool_failed")
                        )
                succeeded = result.get("ok") is not False
                update(
                    "tool_status",
                    {
                        "stage": "tool",
                        "name": call.name,
                        "status": "succeeded" if succeeded else "failed",
                        **(
                            {"error_code": str(result.get("error_code", "tool_failed"))}
                            if not succeeded
                            else {}
                        ),
                    },
                    0,
                )
            except (ValueError, ValidationError):
                result = {"error": "tool_arguments_invalid", "hint": "请按工具 Schema 修正参数"}
                update(
                    "tool_status",
                    {
                        "stage": "tool",
                        "name": call.name,
                        "status": "failed",
                        "error_code": "tool_arguments_invalid",
                    },
                    0,
                )
            result_state["messages"].append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
            result_state["tool_count"] += 1
            result_state["repetitions"][fingerprint] = count
            result_state["tool_cursor"] = cursor + 1
            if save:
                save(result_state, "tools" if cursor + 1 < len(state["calls"]) else "model")
        return result_state

    graph = StateGraph(AgentState)
    graph.add_node("model", model)
    graph.add_node("tools", tools)
    graph.add_conditional_edges(START, lambda _: "tools" if node == "tools" else "model")
    graph.add_conditional_edges("model", lambda state: "tools" if state["calls"] else END)
    graph.add_edge("tools", "model")
    state: AgentState = {
        "messages": messages,
        "text": "",
        "input_tokens": 0,
        "output_tokens": 0,
        "calls": [],
        "step": 0,
        "tool_count": 0,
        "repetitions": {},
        "tool_cursor": 0,
    }
    state = resume or state
    state = {**state, "messages": protect_messages(state["messages"])}
    if _control_prompt:
        state["messages"][0]["content"] += "\n" + _control_prompt
    if node == "done":
        return state
    compiled = graph.compile()
    return cast(
        AgentState, compiled.invoke(state, config={"recursion_limit": 2 * config.max_steps + 3})
    )

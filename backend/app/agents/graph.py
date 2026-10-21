"""以应用契约封装 LangGraph 模型／工具节点，不将图状态或推理链暴露给客户端。"""

import json
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any, NotRequired, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from app.agents.answer_policy import answer_instruction, evidence_policy, validate_references
from app.agents.contracts import AgentConfig
from app.agents.streaming import TextStream
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
        # 闭环模式在普通模型/工具循环外增加规划与检查；内部调用用 _control 避免再次套入。
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
        # Provider 在请求期间也可调用 guard，及时发现取消、租约丢失或权限变化。
        provider.guard = lambda: update(None, None, 0)
        reply: Reply | None = None
        step_text = ""
        # 规划、检查等控制调用必须遵循其 JSON 契约，回答约束仅作用于正文生成。
        policy = evidence_policy(state["messages"]) if _control in {None, "execute"} else None
        output_limit = min(config.max_output_tokens, 1024) if (
            policy and policy["coverage"] != "sufficient"
        ) else config.max_output_tokens
        # 约束在每轮请求前重算，工具返回空证据后不会沿用先前的引用要求。
        request_messages = [
            {key: value for key, value in message.items() if key != "rag_evidence"}
            for message in state["messages"]
        ]
        if policy:
            request_messages[0] = {
                **request_messages[0],
                "content": request_messages[0]["content"] + "\n" + answer_instruction(policy),
            }

        def publish(text: str) -> None:
            """事件与最终正文使用同一批文字，防止缓冲后显示和落库内容不一致。"""
            if not config.closed_loop:
                update("text_delta", {"text": text}, 0)
                on_text(text)

        stream = TextStream(publish)
        with operation_span("model.generate"), trace_step(
            "loop", "模型循环", iteration=state["step"] + 1
        ):
            with (
                nullcontext(Step()) if isinstance(provider, HTTPProvider) else trace_step(
                    "llm", getattr(provider, "model", settings.model_id),
                    provider=getattr(provider, "provider", settings.model_provider),
                )
            ) as request_step:
                try:
                    for event in provider.stream(
                        request_messages, list(config.tools), output_limit
                    ):
                        if event.kind == "text":
                            # 普通模式边生成边发布；闭环模式先积累候选文本，检查后再公开。
                            if (
                                not isinstance(event.text, str)
                                or len((state["text"] + step_text + event.text).encode()) > 65536
                            ):
                                raise ProviderFailure("model_response_too_large")
                            stream.add(event.text)
                            step_text += event.text
                        else:
                            # 最终 Reply 带回工具调用和用量，文本则已在前面的增量事件中收集。
                            reply = event.reply
                            update(None, None, 0)
                    if reply is not None and not isinstance(provider, HTTPProvider):
                        request_step.usage(reply)
                finally:
                    # 包括超时和输出截断在内的异常，也要交付最后一批已接收文字。
                    stream.flush()
        if policy:
            validate_references(step_text, policy)
        if reply is None:
            raise ProviderFailure("model_protocol")
        if not reply.calls and not step_text.strip():
            raise ProviderFailure("model_empty_result")
        # 将模型提出的工具调用写入 assistant 消息，后续 tool 消息通过调用 ID 与它配对。
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
        # 闭环只保留不再调用工具时的候选答复；普通模式累计各轮已公开的文本。
        if config.closed_loop:
            answer_text = step_text if not reply.calls else ""
        else:
            answer_text = state["text"] + step_text
        result: AgentState = {
            **state,
            "step": state["step"] + 1,
            "text": answer_text,
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
            # 恢复时跳过已写入图检查点的工具；未完成的调用仍需再次校验当前权限。
            if cursor < state["tool_cursor"]:
                continue
            update(None, None, 0)
            if call.name not in config.tools:
                raise ProviderFailure("tool_not_authorized")
            if result_state["tool_count"] + 1 > min(config.max_tool_calls, settings.max_tool_calls):
                raise ProviderFailure("tool_call_limit")
            # 同一工具配同一参数累计最多调用两次，避免模型反复尝试同一操作而陷入循环。
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
                # 参数错误作为工具结果交还模型，让下一轮有机会修正，而不是立刻终止任务。
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
            # 工具结果回填上下文，下一轮模型才能根据真实执行结果继续回答。
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

    # 路径为 model → tools → model；模型不再提出工具调用时结束。
    # 恢复入口可直接指向 tools，以继续已保存但尚未执行完的一批调用。
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
    # 固定角色指令在恢复时重新附加，避免旧系统消息反复降级后膨胀上下文。
    from app.guardrails.prompts import SYSTEM_POLICY

    messages = [
        {**message, "content": SYSTEM_POLICY}
        if _control_prompt and message.get("role") == "system"
        and message.get("content") == SYSTEM_POLICY + "\n" + _control_prompt else message
        for message in state["messages"]
    ]
    state = {**state, "messages": protect_messages(messages)}
    if _control_prompt:
        state["messages"][0]["content"] += "\n" + _control_prompt
    if node == "done":
        return state
    compiled = graph.compile()
    return cast(
        AgentState, compiled.invoke(state, config={"recursion_limit": 2 * config.max_steps + 3})
    )

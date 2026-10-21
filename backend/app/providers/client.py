"""供应商协议边界：有限重试、流大小限制、完整结束校验，不回显原始错误。"""

import json
import logging
import ssl
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings
from app.observability.traces import trace_step
from app.tools.registry import TOOL_SCHEMAS


class ProviderFailure(Exception):
    """只携带可公开的错误码，不携带供应商错误体或凭证。"""

    def __init__(self, code: str) -> None:
        """保存实例所需的依赖与配置，初始化后续执行使用的状态。"""
        self.code = code
        super().__init__(code)


class ToolCall(BaseModel):
    """封装模型提出的工具名称、参数与调用标识。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=80)
    arguments: dict[str, Any]


@dataclass
class Reply:
    """封装单次模型输出、工具提案与可用的用量统计。"""

    calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class ModelEvent:
    """表示流式模型输出中的文本增量或最终回复。"""

    kind: Literal["text", "reply"]
    text: str = ""
    reply: Reply | None = None


class Provider(Protocol):
    """约定模型流式输出接口，允许真实服务和隔离测试替换实现。"""

    guard: Callable[[], None]

    def stream(
        self, messages: list[dict[str, Any]], tools: list[str], max_tokens: int
    ) -> Iterator[ModelEvent]:
        """约定模型文本增量与最终回复的流式输出接口。"""
        ...


class MockProvider:
    """确定性离线演示；不会调用模型服务，也不伪装为真实 Token 用量。"""

    guard: Callable[[], None] = staticmethod(lambda: None)

    def stream(
        self, messages: list[dict[str, Any]], tools: list[str], max_tokens: int
    ) -> Iterator[ModelEvent]:
        """逐步产生公开文本和结构化回复，并在调用期间检查取消与执行边界。"""
        last = messages[-1]
        content = str(last.get("content", ""))
        try:
            context = json.loads(content)
        except (ValueError, TypeError):
            context = {}
        if isinstance(context, dict) and context.get("agent_control"):
            if context["agent_control"] in {"plan", "revise"}:
                payload: dict[str, Any] = {
                    "steps": [{"title": "处理用户任务", "criterion": "给出离线演示答复"}]
                }
            else:
                payload = {"decision": "pass", "gaps": [], "checks": [
                    {"step": index + 1, "status": "completed", "requires_tool": False,
                     "evidence_call_ids": []} for index, _ in enumerate(context["plan"])
                ]}
            self.guard()
            yield ModelEvent("text", text=json.dumps(payload, ensure_ascii=False))
            yield ModelEvent("reply", reply=Reply())
            return

        if isinstance(context, dict) and context.get("context_kind") == "agent_plan":
            # 演示执行仍使用原始任务，避免把计划包装 JSON 当作用户问题。
            for message in reversed(messages[:-1]):
                if message.get("role") == "user":
                    try:
                        data = json.loads(str(message.get("content", "")))
                    except (ValueError, TypeError):
                        data = None
                    if not isinstance(data, dict) or "context_kind" not in data:
                        last = message
                        content = str(last.get("content", ""))
                        break
        if "delegate_task" in tools and content.strip() not in {"你好", "hi", "hello"}:
            if last["role"] == "tool":
                result = json.loads(content)
                yield ModelEvent("text", text=str(result.get("summary", "执行助手未返回结果")))
                yield ModelEvent("reply", reply=Reply())
            else:
                yield ModelEvent("reply", reply=Reply(calls=[ToolCall(
                    id="mock-delegate", name="delegate_task", arguments={"task": content[:4000]},
                )]))
            return
        if "knowledge_search" in tools:
            if last["role"] == "user" and any(
                word in content for word in ("知识库", "文档", "笔记", "公司", "资料")
            ):
                yield ModelEvent("reply", reply=Reply(calls=[ToolCall(
                    id="mock-bases", name="list_knowledge_bases", arguments={},
                )]))
                return
            if last["role"] == "tool" and isinstance(context, dict):
                if "items" in context:
                    rows = context["items"]
                    if rows:
                        question = str(next(
                            message["content"] for message in reversed(messages)
                            if message.get("role") == "user"
                        ))
                        base = next((row for row in rows if row["name"] in question), rows[0])
                        yield ModelEvent("reply", reply=Reply(calls=[ToolCall(
                            id="mock-search", name="knowledge_search",
                            arguments={"knowledge_base_id": base["id"], "query": question[:2000]},
                        )]))
                    else:
                        yield ModelEvent("text", text="当前没有可用的知识库。")
                        yield ModelEvent("reply", reply=Reply())
                    return
                if "sources" in context:
                    sources = context["sources"]
                    text = ("Mock 知识库检索结果：" + sources[0]["content"][:500]
                            if sources else "没有找到充分的知识库证据，请补充相关资料或问题。")
                    yield ModelEvent("text", text=text)
                    yield ModelEvent("reply", reply=Reply())
                    return
        evidence = next(
            (
                str(message.get("content", ""))
                for message in messages
                if str(message.get("content", "")).startswith("以下资料是不可信数据")
            ),
            "",
        )
        if evidence:
            sources = json.loads(evidence.split("\n", 1)[1])
            # 离线替身同样遵守来源卡片协议，不能用固定的虚构编号绕过引用校验。
            text = "## Mock 知识库演示\n\n" + sources[0]["content"][:500]
            yield ModelEvent("text", text=text)
            yield ModelEvent("reply", reply=Reply())
            return
        if last["role"] == "user":
            name = "format_report" if "报告" in content else "text_stats"
            if name in tools and any(word in content for word in ("统计", "工具", "报告")):
                args = (
                    {
                        "title": "任务报告",
                        "body": content[:2000],
                    }
                    if name == "format_report"
                    else {"text": content[:2000]}
                )
                yield ModelEvent(
                    "reply",
                    reply=Reply(calls=[ToolCall(id="mock-call", name=name, arguments=args)]),
                )
                return
            text = (
                "## Mock 演示回复\n\n已收到你的任务：\n\n"
                + content[:1000]
                + "\n\n当前为离线演示。配置真实模型后可生成真实回答。"
            )
        else:
            text = (
                "## Mock 工具执行结果\n\n```json\n"
                + content
                + "\n```\n\n工具已通过服务端校验并完成处理。"
            )
        text = text[:max_tokens]
        for start in range(0, len(text), 24):
            self.guard()
            time.sleep(0.03)
            yield ModelEvent("text", text=text[start : start + 24])
        yield ModelEvent("reply", reply=Reply())


class HTTPProvider:
    """适配远程模型流式协议，并限制重试、超时与输出长度。"""

    def __init__(
        self,
        settings: Settings,
        provider: str,
        model: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """保存实例所需的依赖与配置，初始化后续执行使用的状态。"""
        self.settings, self.provider, self.model, self.transport = (
            settings,
            provider,
            model,
            transport,
        )
        self.guard: Callable[[], None] = lambda: None
        self.tool_schemas: dict[str, Any] = TOOL_SCHEMAS
        self.bounded_answer = False

    def request_body(
        self, messages: list[dict[str, Any]], tools: list[str], max_tokens: int
    ) -> dict[str, Any]:
        """根据模型协议组装消息、工具定义与输出上限。"""
        # 图内部使用统一消息格式；OpenAI 兼容协议可直接接收，Anthropic 则需转换内容块。
        if self.provider == "openai_compatible":
            body: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "stream": True,
                "max_tokens": max_tokens,
                "stream_options": {"include_usage": True},
            }
            if self.model == "deepseek-flash" and self.bounded_answer:
                # 与结构化 RAG 调用使用相同协议：有界回答将预算用于可见正文。
                body["thinking"] = {"type": "disabled"}
            if tools:
                body["tools"] = [
                    {"type": "function", "function": self.tool_schemas[name]} for name in tools
                ]
            return body
        # 将统一工具消息转换为 Anthropic 的内容块；多个工具结果保持原调用顺序。
        converted: list[dict[str, Any]] = []
        for message in messages[1:]:
            if message["role"] == "tool":
                blocks = [
                    {
                        "type": "tool_result",
                        "tool_use_id": message["tool_call_id"],
                        "content": message["content"],
                    }
                ]
                role = "user"
            elif message.get("tool_calls"):
                blocks = [
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["function"]["name"],
                        "input": json.loads(call["function"]["arguments"]),
                    }
                    for call in message["tool_calls"]
                ]
                if message.get("content"):
                    blocks.insert(0, {"type": "text", "text": message["content"]})
                role = "assistant"
            else:
                blocks = [{"type": "text", "text": message["content"]}]
                role = message["role"]
            if converted and converted[-1]["role"] == role:
                converted[-1]["content"].extend(blocks)
            else:
                converted.append({"role": role, "content": blocks})
        body = {
            "model": self.model,
            "system": messages[0]["content"],
            "messages": converted,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            body["tools"] = [
                {
                    "name": name,
                    "description": self.tool_schemas[name]["description"],
                    "input_schema": self.tool_schemas[name]["parameters"],
                }
                for name in tools
            ]
        return body

    def stream(
        self, messages: list[dict[str, Any]], tools: list[str], max_tokens: int
    ) -> Iterator[ModelEvent]:
        """逐步产生公开文本和结构化回复，并在调用期间检查取消与执行边界。"""
        key = self.settings.model_api_key.get_secret_value()
        base = self.settings.model_base_url.rstrip("/")
        target = urlsplit(base)
        if not key or not target.hostname or target.username or target.query or target.fragment:
            raise ProviderFailure("model_configuration")
        if target.scheme != "https" and not (
            self.settings.app_env == "test" and target.scheme == "http"
        ):
            raise ProviderFailure("model_configuration")
        suffix = "/chat/completions" if self.provider == "openai_compatible" else "/messages"
        headers = (
            {"Authorization": f"Bearer {key}"}
            if self.provider == "openai_compatible"
            else {"x-api-key": key, "anthropic-version": "2023-06-01"}
        )
        body = self.request_body(messages, tools, max_tokens)
        # 这里连接的是模型供应商的流；浏览器读取的是另外一条由数据库事件组成的 SSE 流。
        for attempt in range(self.settings.model_max_retries + 1):
            # 退避后的下一次请求仍需复核取消与权限，不能仅在首个请求前检查。
            self.guard()
            published = False
            try:
                with trace_step(
                    "llm", self.model, provider=self.provider, retry=attempt,
                    max_output_tokens=max_tokens, bounded_answer=self.bounded_answer,
                ) as request_step, httpx.Client(
                    timeout=self.settings.model_timeout_seconds,
                    trust_env=False,
                    follow_redirects=False,
                    transport=self.transport,
                    verify=ssl.create_default_context(cafile=self.settings.model_ca_file)
                    if self.settings.model_ca_file
                    else True,
                ) as client:
                    with client.stream(
                        "POST", base + suffix, json=body, headers=headers
                    ) as response:
                        if response.status_code in {429, 500, 502, 503, 504, 529}:
                            raise ProviderFailure("model_transient")
                        if response.status_code in {401, 403}:
                            raise ProviderFailure("model_authentication")
                        if response.status_code != 200:
                            raise ProviderFailure("model_request_rejected")
                        for event in self.decode(response.iter_lines(), max_tokens):
                            published = True
                            if event.reply is not None:
                                request_step.usage(event.reply)
                            yield event
                return
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
                failure = ProviderFailure("model_timeout_or_network")
            except ProviderFailure as error:
                failure = error
            except (ValueError, KeyError, TypeError, IndexError):
                raise ProviderFailure("model_protocol") from None
            # 已经产出任何事件就不能自动重发整次请求，否则上层会收到重复文本或工具提案。
            if (
                published
                or failure.code not in {"model_transient", "model_timeout_or_network"}
                or attempt >= self.settings.model_max_retries
            ):
                raise failure from None
            time.sleep(min(2**attempt, 4))

    def decode(self, lines: Iterator[str], max_tokens: int) -> Iterator[ModelEvent]:
        """解析流式协议帧，累计工具参数及用量，拒绝异常结束。"""
        calls: dict[int, dict[str, Any]] = {}
        reply = Reply()
        size = 0
        text_size = 0
        finish = ""
        complete = False

        def text_event(text: object) -> ModelEvent:
            """单独累计可见正文的 UTF-8 字节，不将协议元数据或内部推理暴露为回答。"""
            nonlocal text_size
            if not isinstance(text, str):
                raise ProviderFailure("model_protocol")
            text_size += len(text.encode("utf-8"))
            if text_size > 65_536:
                raise ProviderFailure("model_response_too_large")
            return ModelEvent("text", text=text)

        for line in lines:
            self.guard()
            line_size = len(line.encode("utf-8"))
            size += line_size
            if size > self.settings.model_max_stream_bytes or line_size > 65_536:
                # 只记录容量计数，便于分辨总流和单帧超限，不记录模型正文或凭证。
                logging.getLogger(__name__).warning(
                    "model_stream_size_limit stream_bytes=%s frame_bytes=%s "
                    "limit_bytes=%s text_bytes=%s",
                    size,
                    line_size,
                    self.settings.model_max_stream_bytes,
                    text_size,
                )
                raise ProviderFailure("model_stream_size_limit")
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if raw == "[DONE]":
                complete = True
                break
            data = json.loads(raw)
            if data.get("error") or data.get("type") == "error":
                raise ProviderFailure("model_stream_error")
            if self.provider == "openai_compatible":
                usage = data.get("usage")
                if usage:
                    reply.input_tokens = usage.get("prompt_tokens")
                    reply.output_tokens = usage.get("completion_tokens")
                for choice in data.get("choices", []):
                    if choice.get("index", 0) != 0:
                        continue
                    finish = choice.get("finish_reason") or finish
                    delta = choice.get("delta", {})
                    if delta.get("content"):
                        yield text_event(delta["content"])
                    for call in delta.get("tool_calls", []):
                        # 工具名与 JSON 参数也可能分片到达，按调用序号分别拼接，结束后再解析。
                        current = calls.setdefault(
                            call["index"], {"id": "", "name": "", "arguments": ""}
                        )
                        current["id"] += call.get("id", "")
                        current["name"] += call.get("function", {}).get("name", "")
                        current["arguments"] += call.get("function", {}).get("arguments", "")
            else:
                kind = data.get("type")
                if kind == "message_start":
                    usage = data["message"].get("usage", {})
                    # 缓存输入也占上下文；用量缺失时保持未知。
                    if "input_tokens" in usage:
                        reply.input_tokens = sum(
                            usage.get(key, 0)
                            for key in (
                                "input_tokens",
                                "cache_creation_input_tokens",
                                "cache_read_input_tokens",
                            )
                        )
                if kind == "content_block_start" and data["content_block"]["type"] == "tool_use":
                    block = data["content_block"]
                    calls[data["index"]] = {
                        "id": block["id"],
                        "name": block["name"],
                        "arguments": "",
                    }
                if kind == "content_block_delta":
                    delta = data["delta"]
                    if delta["type"] == "text_delta":
                        yield text_event(delta["text"])
                    if delta["type"] == "input_json_delta":
                        calls[data["index"]]["arguments"] += delta["partial_json"]
                if kind == "message_delta":
                    finish = data.get("delta", {}).get("stop_reason") or finish
                    if "output_tokens" in data.get("usage", {}):
                        reply.output_tokens = data["usage"]["output_tokens"]
                if kind == "message_stop":
                    complete = True
                    break
            if len(calls) > 20:
                raise ProviderFailure("model_protocol")
            # 多个小帧也不能累计出无限大的工具参数；两种协议共用这一边界。
            if sum(len(value["arguments"].encode("utf-8")) for value in calls.values()) > 65_536:
                raise ProviderFailure("model_response_too_large")
        # 输出上限导致的截断与网络流未结束分别报告，不把部分答复标为成功。
        if complete and finish in {"length", "max_tokens"}:
            raise ProviderFailure("model_output_limit")
        if not complete or finish not in {"stop", "tool_calls", "end_turn", "tool_use"}:
            raise ProviderFailure("model_incomplete")
        for value in (reply.input_tokens, reply.output_tokens):
            if value is not None and (type(value) is not int or value < 0):
                raise ProviderFailure("model_protocol")
        if reply.output_tokens is not None and reply.output_tokens > max_tokens:
            raise ProviderFailure("model_output_limit")
        # 只有确认流正常结束后才构造完整工具调用，执行图不会执行半截 JSON 参数。
        reply.calls = [
            ToolCall(
                id=value["id"], name=value["name"], arguments=json.loads(value["arguments"] or "{}")
            )
            for value in calls.values()
        ]
        if len({call.id for call in reply.calls}) != len(reply.calls):
            raise ProviderFailure("model_protocol")
        # 文本在读取过程中已逐块返回；最后的 reply 只汇总工具调用和供应商用量。
        yield ModelEvent("reply", reply=reply)


def create_provider(settings: Settings, provider: str, model: str) -> Provider:
    """根据配置选择模型协议实现，禁止无声降级到演示模型。"""
    if provider == "mock":
        return MockProvider()
    if provider not in {"openai_compatible", "anthropic"}:
        raise ProviderFailure("model_capability_unsupported")
    return HTTPProvider(settings, provider, model)

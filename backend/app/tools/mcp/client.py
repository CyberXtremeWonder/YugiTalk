"""有界远程 MCP 客户端；复用固定公网 IP/TLS 连接，禁止重定向与自动重试。"""

import json
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Any, cast
from urllib.parse import urljoin, urlsplit

from app.providers.client import ProviderFailure
from app.tools.http.contracts import public_endpoint
from app.tools.http.runtime import PinnedHTTPSConnection, resolve_public

LIMIT = 1024 * 1024
VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}


class MCPClient(AbstractContextManager["MCPClient"]):
    """每次操作独立连接与认证上下文，不跨用户共享会话。"""

    def __init__(
        self, config: dict[str, Any], token: str = "", guard: Callable[[], None] = lambda: None
    ):
        self.url = public_endpoint(config["url"])
        self.transport = config["transport"]
        self.guard = guard
        self.deadline = time.monotonic() + 60
        self.headers = {"Accept": "application/json, text/event-stream"}
        if token:
            self.headers["Authorization"] = "Bearer " + token
        self.connections: list[PinnedHTTPSConnection] = []
        self.events: Iterator[tuple[str, str]] | None = None
        self.post_url = self.url
        self.sequence = 0
        self.received = 0

    def check(self) -> None:
        self.guard()
        if time.monotonic() >= self.deadline:
            raise ProviderFailure("mcp_timeout")

    def request(self, method: str, url: str, body: dict[str, Any] | None = None) -> Any:
        self.check()
        target, origin = urlsplit(url), urlsplit(self.url)
        # SSE endpoint 允许同源查询串（会话 ID），但不能指向其他源或注入头部。
        if (
            (target.scheme, target.hostname, target.port or 443)
            != (origin.scheme, origin.hostname, origin.port or 443)
            or target.username
            or target.password
            or target.fragment
            or any(c.isspace() for c in url)
            or "\\" in url
        ):
            raise ProviderFailure("mcp_endpoint_denied")
        host = target.hostname or ""
        address = resolve_public(host, self.deadline)
        connection = PinnedHTTPSConnection(host, address, min(10, self.deadline - time.monotonic()))
        self.connections.append(connection)
        headers = {**self.headers, "Accept-Encoding": "identity"}
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
            headers["Content-Type"] = "application/json"
        path = (target.path or "/") + ("?" + target.query if target.query else "")
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        if response.status not in {200, 202, 204}:
            if response.status == 404:
                raise ProviderFailure("mcp_not_found")
            if response.status in {405, 406, 415}:
                raise ProviderFailure("mcp_transport_mismatch")
            raise ProviderFailure(
                "mcp_auth_required" if response.status in {401, 403} else "mcp_http_failed"
            )
        if response.getheader("Content-Encoding", "identity") != "identity":
            raise ProviderFailure("mcp_response_invalid")
        session = response.getheader("Mcp-Session-Id")
        if session:
            if len(session) > 256 or not all(33 <= ord(c) <= 126 for c in session):
                raise ProviderFailure("mcp_response_invalid")
            self.headers["Mcp-Session-Id"] = session
        return response

    def chunks(self, response: Any) -> Iterator[bytes]:
        while True:
            self.check()
            value = response.read1(4096)
            if not value:
                break
            self.received += len(value)
            if self.received > LIMIT:
                raise ProviderFailure("mcp_response_too_large")
            yield value

    def stream(self, response: Any) -> Iterator[tuple[str, str]]:
        pending = b""
        event = "message"
        data: list[str] = []
        for chunk in self.chunks(response):
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                text = line.rstrip(b"\r").decode("utf-8")
                if not text:
                    if data:
                        yield event, "\n".join(data)
                    event, data = "message", []
                elif text.startswith("event:"):
                    event = text[6:].lstrip(" ")
                elif text.startswith("data:"):
                    data.append(text[5:].removeprefix(" "))

    def __enter__(self) -> "MCPClient":
        try:
            if self.transport == "sse":
                response = self.request("GET", self.url)
                if "text/event-stream" not in response.getheader("Content-Type", ""):
                    raise ProviderFailure("mcp_response_invalid")
                self.events = self.stream(response)
                for event, data in self.events:
                    if event == "endpoint":
                        self.post_url = urljoin(self.url, data)
                        break
                else:
                    raise ProviderFailure("mcp_response_invalid")
            result = self.rpc(
                "initialize",
                {
                    "protocolVersion": "2024-11-05" if self.transport == "sse" else "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "YugiTalk", "version": "0.1.0"},
                },
            )
            version = result.get("protocolVersion")
            if version not in VERSIONS:
                raise ProviderFailure("mcp_protocol_unsupported")
            if (
                not isinstance(result.get("capabilities"), dict)
                or "tools" not in result["capabilities"]
            ):
                raise ProviderFailure("mcp_tools_unsupported")
            self.headers["MCP-Protocol-Version"] = version
            self.rpc("notifications/initialized", notification=True)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def rpc(
        self, method: str, params: dict[str, Any] | None = None, notification: bool = False
    ) -> dict[str, Any]:
        self.sequence += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notification:
            message["id"] = self.sequence
        response = self.request("POST", self.post_url, message)
        if notification:
            response.close()
            return {}
        if self.events is not None:
            response.close()
            events = self.events
        elif "text/event-stream" in response.getheader("Content-Type", ""):
            events = self.stream(response)
        elif "application/json" in response.getheader("Content-Type", ""):
            events = iter([("message", b"".join(self.chunks(response)).decode("utf-8"))])
        else:
            raise ProviderFailure("mcp_response_invalid")
        for event, value in events:
            if event != "message":
                continue
            result = json.loads(value)
            if not isinstance(result, dict) or result.get("jsonrpc") != "2.0":
                raise ProviderFailure("mcp_response_invalid")
            if "method" in result:
                if "id" in result:
                    reply = {"jsonrpc": "2.0", "id": result["id"]}
                    if result["method"] == "ping":
                        reply["result"] = {}
                    else:
                        reply["error"] = {"code": -32601, "message": "Method not supported"}
                    self.request("POST", self.post_url, reply).close()
                continue
            if type(result.get("id")) is not int or result.get("id") != self.sequence:
                continue
            if "error" in result:
                raise ProviderFailure("mcp_rpc_failed")
            if not isinstance(result.get("result"), dict):
                raise ProviderFailure("mcp_response_invalid")
            return cast(dict[str, Any], result["result"])
        raise ProviderFailure("mcp_connection_closed")

    def tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor = None
        for _ in range(10):
            result = self.rpc("tools/list", {"cursor": cursor} if cursor else {})
            if not isinstance(result.get("tools"), list):
                raise ProviderFailure("mcp_response_invalid")
            tools.extend(result["tools"])
            if len(tools) > 100:
                raise ProviderFailure("mcp_tool_limit")
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
            if not isinstance(cursor, str) or len(cursor) > 2048:
                raise ProviderFailure("mcp_response_invalid")
        raise ProviderFailure("mcp_tool_limit")

    def __exit__(self, *args: object) -> None:
        for connection in self.connections:
            connection.close()

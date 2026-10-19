"""声明式 HTTPS 查询执行：域名白名单、DNS 固定、TLS 验证及有界 JSON 输出。"""

import http.client
import ipaddress
import json
import math
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Any, cast
from urllib.parse import urlencode, urlsplit

from app.core.config import Settings
from app.providers.client import ProviderFailure
from app.tools.default_http import DEFAULT_HTTP_NAMES
from app.tools.management_contracts import ToolInput
from app.tools.service import readiness, secret_value, validate_config
from app.tools.web_search import WebSearch, search_result

DNS_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tool-dns")
DNS_SLOTS = threading.BoundedSemaphore(4)


def validate_arguments(config: ToolInput, arguments: object) -> dict[str, Any]:
    """严格校验必填项、标量类型和字节数，不接受额外字段或无限嵌套数据。"""
    if not isinstance(arguments, dict):
        raise ValueError("工具参数必须是对象")
    definitions = {item.name: item for item in config.parameters}
    if any(name not in definitions for name in arguments):
        raise ValueError("工具参数包含未声明字段")
    for name, definition in definitions.items():
        if name not in arguments:
            if definition.required:
                raise ValueError("工具缺少必填参数")
            continue
        value = arguments[name]
        valid = {
            "string": isinstance(value, str) and len(value.encode()) <= 2000,
            "integer": type(value) is int and abs(value) <= 9_000_000_000_000_000,
            "number": type(value) in {int, float}
            and abs(value) <= 9_000_000_000_000_000
            and math.isfinite(value),
            "boolean": type(value) is bool,
        }[definition.type]
        if not valid:
            raise ValueError("工具参数类型或长度不合法")
    if len(json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode()) > 8000:
        raise ValueError("工具参数过大")
    return arguments


def resolve_public(host: str, deadline: float) -> str:
    """有界并发解析 DNS，拒绝混合私网解析，并把已校验的地址固定给实际连接。"""
    if not DNS_SLOTS.acquire(blocking=False):
        raise ProviderFailure("tool_dns_busy")

    def lookup() -> list[Any]:
        """解析工作占用独立槽位；即使请求已超时，工作结束才释放以避免无界排队。"""
        try:
            return socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        finally:
            DNS_SLOTS.release()

    future = DNS_POOL.submit(lookup)
    try:
        rows = future.result(timeout=max(0.01, min(2.0, deadline - time.monotonic())))
    except TimeoutError:
        raise ProviderFailure("tool_dns_timeout") from None
    except OSError:
        raise ProviderFailure("tool_network_failed") from None
    addresses = list(dict.fromkeys(str(row[4][0]) for row in rows))
    if (
        not addresses
        or len(addresses) > 8
        or any(not ipaddress.ip_address(value).is_global for value in addresses)
    ):
        raise ProviderFailure("tool_network_denied")
    return addresses[0]


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """连接已校验的 IP，但 TLS 的 SNI、证书校验和 HTTP Host 仍使用原域名。"""

    def __init__(self, host: str, address: str, timeout: float) -> None:
        """固定唯一目标与受信任 TLS 上下文，不读取系统代理或跳过证书验证。"""
        self.tls_context = ssl.create_default_context()
        super().__init__(host, port=443, timeout=timeout, context=self.tls_context)
        self.address = address
        self.deadline = time.monotonic() + timeout

    def connect(self) -> None:
        """直接连接固定 IP，禁止二次域名解析导致 DNS 重绑定。"""
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        connection = socket.create_connection((self.address, 443), remaining)
        try:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            # TCP 连接消耗的时间不能在 TLS 握手时重新获得一份完整超时。
            connection.settimeout(remaining)
            self.sock = self.tls_context.wrap_socket(connection, server_hostname=self.host)
        except Exception:
            connection.close()
            raise


def execute_http(
    pin: dict[str, Any], arguments: object, settings: Settings, guard: Callable[[], None]
) -> dict[str, object]:
    """调用管理员声明的只读查询，逐块检查取消、截止时间、响应类型和容量。"""
    config = validate_config(pin["config"])
    deadline = time.monotonic() + config.timeout_seconds
    result = _execute_http(pin, arguments, settings, guard, deadline, raw_content=True)
    if (
        pin.get("builtin")
        and config.name == "web_search"
        and result.get("error_code") == "tool_result_too_large"
    ):
        # 完整正文过大时仅降级一次；共享原截止时间，不占用新的模型轮次。
        guard()
        result = _execute_http(pin, arguments, settings, guard, deadline, raw_content=False)
        if result.get("ok") is True:
            result["content_mode"] = "snippets"
            result["raw_content_omitted"] = True
    return result


def _execute_http(
    pin: dict[str, Any],
    arguments: object,
    settings: Settings,
    guard: Callable[[], None],
    deadline: float,
    *,
    raw_content: bool,
) -> dict[str, object]:
    """执行单次有界请求，降级请求必须沿用原始截止时间与授权检查。"""
    config = validate_config(pin["config"])
    search = bool(pin.get("builtin") and config.name == "web_search")
    data = (
        WebSearch.model_validate(arguments).model_dump()
        if search
        else validate_arguments(config, arguments)
    )
    response_limit = 2 * 1024 * 1024 if search else config.max_response_bytes
    if pin.get("builtin") and config.name in DEFAULT_HTTP_NAMES:
        code = data.get("city" if config.name == "weather_query" else "adcode", "")
        if not re.fullmatch(r"[0-9]{6}", code):
            raise ValueError("城市编码必须为六位数字")
        if config.name == "weather_query" and data.get("extensions", "base") not in {"base", "all"}:
            raise ValueError("天气类型必须为 base 或 all")
    if readiness(pin["config"], settings):
        raise ProviderFailure("tool_not_configured")
    endpoint = urlsplit(config.endpoint)
    assert endpoint.hostname
    guard()
    address = resolve_public(endpoint.hostname, deadline)
    guard()
    secret = secret_value(pin["config"], settings)
    if secret and (len(secret) > 4096 or "\r" in secret or "\n" in secret):
        raise ProviderFailure("tool_not_configured")
    query_data = {
        key: str(value).lower() if type(value) is bool else str(value)
        for key, value in data.items()
    }
    # 只有固定内置接口采用高德要求的 key 参数；用户声明的工具始终使用 Bearer。
    amap = pin.get("builtin") and config.name in DEFAULT_HTTP_NAMES
    if amap:
        query_data.update({"key": secret, "output": "JSON"})
    query = "" if search else urlencode(query_data)
    target = (endpoint.path or "/") + ("?" + query if query else "")
    if len(target.encode()) > 8192:
        raise ValueError("工具查询地址过长")
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "User-Agent": "YugiTalk-Tool/1.0",
    }
    if secret and not amap:
        headers["Authorization"] = "Bearer " + secret
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProviderFailure("tool_timeout")
    connection = PinnedHTTPSConnection(endpoint.hostname, address, remaining)
    expired = threading.Event()

    def abort() -> None:
        """到期主动中断套接字，避免响应头逐字到达时不断重置读取超时。"""
        expired.set()
        if connection.sock:
            try:
                connection.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        connection.close()

    watchdog = threading.Timer(remaining, abort)
    watchdog.daemon = True
    watchdog.start()
    try:
        if search:
            headers["Content-Type"] = "application/json"
            body = json.dumps(
                {
                    **data,
                    "search_depth": "basic",
                    "topic": "general",
                    "include_raw_content": "text" if raw_content else False,
                    "include_answer": False,
                    "include_images": False,
                    "auto_parameters": False,
                },
                ensure_ascii=False,
            ).encode("utf-8")
            connection.request("POST", target, body=body, headers=headers)
        else:
            connection.request("GET", target, headers=headers)
        if connection.sock:
            connection.sock.settimeout(max(0.01, deadline - time.monotonic()))
        response = connection.getresponse()
        if response.status != 200:
            return {"ok": False, "error_code": "tool_http_failed"}
        if response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() not in {
            "application/json",
            "application/problem+json",
        } or response.getheader("Content-Encoding", "identity").lower() not in {"identity", ""}:
            return {"ok": False, "error_code": "tool_response_invalid"}
        content = bytearray()
        while True:
            guard()
            if time.monotonic() >= deadline:
                raise ProviderFailure("tool_timeout")
            if connection.sock:
                connection.sock.settimeout(max(0.01, deadline - time.monotonic()))
            chunk = response.read1(min(4096, response_limit + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > response_limit:
                return {"ok": False, "error_code": "tool_result_too_large"}
        guard()
        if time.monotonic() >= deadline:
            raise ProviderFailure("tool_timeout")
        raw = content.decode("utf-8")
        if secret and secret in raw:
            return {"ok": False, "error_code": "tool_result_sensitive"}
        result = json.loads(raw)
        if amap and (not isinstance(result, dict) or result.get("status") != "1"):
            return {"ok": False, "error_code": "tool_provider_rejected"}
        if amap:
            field = (
                ("forecasts" if data.get("extensions") == "all" else "lives")
                if config.name == "weather_query"
                else "trafficinfo"
            )
            evidence = result.get(field)
            if not evidence or not isinstance(
                evidence, list if config.name == "weather_query" else dict
            ):
                return {"ok": False, "error_code": "tool_no_data"}
        if search:
            result = search_result(result, data["max_results"])
        # 空白与压缩格式不能绕过进入模型上下文的规范化 JSON 容量。
        normalized = json.dumps(result, ensure_ascii=False, allow_nan=False)
        if secret and secret in normalized:
            return {"ok": False, "error_code": "tool_result_sensitive"}
        if len(normalized.encode()) > config.max_response_bytes:
            return {"ok": False, "error_code": "tool_result_too_large"}
        if search:
            return cast(dict[str, object], result)
        return {"ok": True, "data": result, **({"source": "高德地图"} if amap else {})}
    except TimeoutError:
        raise ProviderFailure("tool_timeout") from None
    except (OSError, http.client.HTTPException):
        if expired.is_set():
            raise ProviderFailure("tool_timeout") from None
        return {"ok": False, "error_code": "tool_network_failed"}
    except (ValueError, UnicodeError, RecursionError):
        return {"ok": False, "error_code": "tool_response_invalid"}
    finally:
        watchdog.cancel()
        connection.close()

"""工具目录、快照、缓存撤销与安全 HTTP 执行回归，所有网络响应均隔离模拟。"""

import copy
import json
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from app.core.config import Settings
from app.providers.client import HTTPProvider, ModelEvent, ProviderFailure, Reply, ToolCall
from app.tasks.execution import Execution
from app.tasks.ledger_models import ToolExecution
from app.tasks.models import Run
from app.tasks.reliability import execute_bound_tool
from app.tools import http_runtime
from app.tools.default_http import default_config
from app.tools.http_runtime import execute_http, resolve_public, validate_arguments
from app.tools.management_contracts import ToolInput, public_endpoint
from app.tools.models import ManagedTool, ToolVersion
from app.tools.service import bound_tool, freeze_tools
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_agents import setup_task
from test_identity import ORIGIN, login

pytest_plugins = ["test_identity"]


def tool_data(**changes: Any) -> dict[str, Any]:
    """构造不含真实凭证的最小查询声明，测试时不访问示例域名。"""
    return {
        "name": "public_lookup",
        "title": "公开查询",
        "description": "只读检索公开记录",
        "endpoint": "https://api.example.com/search",
        "parameters": [{"name": "q", "type": "string", "required": True}],
        **changes,
    }


def context(client: Any, email: str = "owner@example.test") -> tuple[str, str, dict[str, str]]:
    """返回隔离用户、空间和 CSRF 请求头，不依赖真实业务身份。"""
    identity = login(client, email)
    return (
        identity["user"]["id"],
        identity["user"]["id"],
        {
            **ORIGIN,
            "X-CSRF-Token": identity["csrf_token"],
        },
    )


def test_catalog_and_crud(client: Any) -> None:
    """默认五项仅读取不落库，管理员 CRUD 追加版本且删除后立即隐藏。"""
    space, _, headers = context(client)
    path = f"/api/v1/me/tools"
    items = client.get(path).json()["items"]
    assert {item["name"] for item in items} == {
        "text_stats",
        "format_report",
        "weather_query",
        "traffic_query",
        "web_search",
    }
    assert all(item["builtin"] for item in items)
    assert all(not item["ready"] for item in items if item["kind"] == "http")
    with Session(client.app.state.database) as db:
        assert db.scalar(select(func.count()).select_from(ManagedTool)) == 0
    data = tool_data()
    response = client.post(path, json=data, headers=headers)
    assert response.status_code == 201, response.text
    first = response.json()
    assert first["number"] == 1
    assert client.post(path, json=data, headers=headers).status_code == 409
    second = client.post(
        path + "/public_lookup/versions", json=tool_data(title="查询二版"), headers=headers
    )
    assert second.status_code == 201 and second.json()["number"] == 2
    with Session(client.app.state.database) as db:
        assert db.get(ToolVersion, first["version_id"]).config["title"] == "公开查询"
    valid = client.post(path + "/public_lookup/validate", json={"q": "输入"}, headers=headers)
    assert valid.json() == {"valid": True, "network_called": False}
    assert (
        client.post(
            path + "/public_lookup/validate", json={"extra": "值"}, headers=headers
        ).status_code
        == 422
    )
    assert (
        client.patch(path + "/public_lookup", json={"enabled": False}, headers=headers).json()[
            "enabled"
        ]
        is False
    )
    assert client.delete(path + "/public_lookup", headers=headers).status_code == 204
    assert all(item["name"] != "public_lookup" for item in client.get(path).json()["items"])
    assert client.delete(path + "/weather_query", headers=headers).status_code == 409
    assert (
        client.post(path, json=tool_data(name="weather_query"), headers=headers).status_code == 409
    )
    assert (
        client.patch(path + "/weather_query", json={"enabled": False}, headers=headers).status_code
        == 200
    )
    assert next(item for item in client.get(path).json()["items"]
                if item["name"] == "weather_query")["enabled"] is False


def test_member_and_cross_workspace_denied(client: Any) -> None:
    """普通成员与另一空间不能维护工具或向助手注入未注册工具。"""
    space, _, headers = context(client)
    path = f"/api/v1/me/tools"
    assert client.post(path, json=tool_data(), headers=headers).status_code == 201
    _, _, member_headers = context(client, "member@example.test")
    assert client.get(path).status_code == 200
    for method, suffix, data in [
        ("POST", "", tool_data(name="other_tool")),
        ("PATCH", "/public_lookup", {"enabled": False}),
        ("DELETE", "/public_lookup", None),
        ("POST", "/public_lookup/validate", {"q": "值"}),
    ]:
        assert (
            client.request(method, path + suffix, json=data, headers=member_headers).status_code
            == (403 if method == "POST" and not suffix else 404)
        )
    other, _, other_headers = context(client, "other@example.test")
    assert client.get(path).status_code == 200
    assert (
        client.post(
            f"/api/v1/me/agents",
            json={"name": "越权", "tools": ["public_lookup"]},
            headers=other_headers,
        ).status_code
        == 422
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://api.example.com/search",
        "https://127.0.0.1",
        "https://169.254.169.254/latest",
        "https://10.0.0.1",
        "https://[::1]",
        "https://user:pass@api.example.com",
        "https://api.example.com:444",
        "https://localhost",
        "https://api.internal",
        "https://api.example.com/?key=secret",
        "https://api.example.com/#x",
        "https://api.example.com./",
        "https://api.example.com/\n",
    ],
)
def test_endpoint_rejects_ssrf(endpoint: str) -> None:
    """拒绝静态 SSRF、凭证 URL 和可变查询串，真实域名另做 DNS 固定。"""
    with pytest.raises(ValueError):
        public_endpoint(endpoint)


@pytest.mark.parametrize(
    "changes",
    [
        {"method": "POST"},
        {"secret": "raw-key"},
        {"parameters": [{"name": "q", "type": "object"}]},
        {"parameters": [{"name": "q"}, {"name": "q"}]},
        {"required_roles": []},
        {"timeout_seconds": 21},
        {"max_response_bytes": 16001},
    ],
)
def test_declared_contract_is_bounded(changes: dict[str, Any]) -> None:
    """工具配置不能携带脚本、真实密钥、嵌套输入或无界运行策略。"""
    with pytest.raises(ValidationError):
        ToolInput.model_validate(tool_data(**changes))


def test_argument_types_and_bounds() -> None:
    """严格区分布尔与整数并拒绝缺字段、额外字段和大文本。"""
    config = ToolInput.model_validate(tool_data())
    for data in [{}, {"q": 4}, {"q": "x" * 2001}, {"q": "值", "extra": 1}, []]:
        with pytest.raises(ValueError):
            validate_arguments(config, data)
    number = ToolInput.model_validate(tool_data(parameters=[{"name": "n", "type": "integer"}]))
    with pytest.raises(ValueError):
        validate_arguments(number, {"n": True})


def test_pins_preserve_version_and_reject_tamper_and_revocation(client: Any) -> None:
    """旧快照保持原端点，篡改与新版角色收窄、停用、删除均不可绕过。"""
    space, user, headers = context(client)
    path = f"/api/v1/me/tools"
    client.post(path, json=tool_data(), headers=headers)
    settings = Settings(_env_file=None, tool_http_allowed_hosts=["api.example.com"])
    with Session(client.app.state.database) as db:
        pins = freeze_tools(db, space, user, ["public_lookup", "weather_query"], settings)
        assert set(pins) == {"public_lookup"}
    snapshot = {"tools": ["public_lookup"], "tool_catalog": pins}
    client.post(
        path + "/public_lookup/versions",
        json=tool_data(endpoint="https://api.example.com/second"),
        headers=headers,
    )
    with Session(client.app.state.database) as db:
        assert bound_tool(db, space, user, snapshot, "public_lookup", settings)["config"][
            "endpoint"
        ].endswith("/search")
        altered = copy.deepcopy(snapshot)
        altered["tool_catalog"]["public_lookup"]["schema"]["description"] = "篡改"
        with pytest.raises(ProviderFailure, match="tool_content_changed"):
            bound_tool(db, space, user, altered, "public_lookup", settings)
    client.post(
        path + "/public_lookup/versions", json=tool_data(required_roles=["admin"]), headers=headers
    )
    with (
        Session(client.app.state.database) as db,
        pytest.raises(ProviderFailure, match="tool_not_authorized"),
    ):
        bound_tool(db, space, user, snapshot, "public_lookup", settings)
    client.patch(path + "/public_lookup", json={"enabled": False}, headers=headers)
    with (
        Session(client.app.state.database) as db,
        pytest.raises(ProviderFailure, match="tool_unavailable"),
    ):
        bound_tool(db, space, user, snapshot, "public_lookup", settings)
    client.delete(path + "/public_lookup", headers=headers)
    with (
        Session(client.app.state.database) as db,
        pytest.raises(ProviderFailure, match="tool_unavailable"),
    ):
        bound_tool(db, space, user, snapshot, "public_lookup", settings)


def test_cached_builtin_is_not_reused_when_disabled(client: Any) -> None:
    """成功账本也必须经过当前启停授权，停用不会被缓存结果复活。"""
    headers, _, task, _ = setup_task(client, tools=["text_stats"])
    call = ToolCall(id="fixed", name="text_stats", arguments={"text": "你好"})
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        space = run.account_id
        assert execute_bound_tool(db, run, call, 1)["characters"] == 2
        db.commit()
    client.patch(
        f"/api/v1/me/tools/text_stats", json={"enabled": False}, headers=headers
    )
    with (
        Session(client.app.state.database) as db,
        pytest.raises(ProviderFailure, match="tool_unavailable"),
    ):
        execute_bound_tool(db, db.get(Run, task["id"]), call, 1)


def test_task_freezes_custom_and_skill_intersection(client: Any) -> None:
    """目录注册不授予权限，任务仅冻结助手与技能交集中的已就绪工具。"""
    space, _, headers = context(client)
    client.app.state.settings.tool_http_allowed_hosts = ["api.example.com"]
    client.post(f"/api/v1/me/tools", json=tool_data(), headers=headers)
    headers, body, task, _ = setup_task(
        client, tools=["public_lookup", "text_stats", "weather_query"]
    )
    with Session(client.app.state.database) as db:
        assert db.get(Run, task["id"]).snapshot["tools"] == ["public_lookup", "text_stats"]
    client.post(f"/api/v1/runs/{task['id']}/cancel", headers=headers)
    skill = client.post(
        f"/api/v1/me/skills",
        json={
            "name": "no-tools",
            "description": "仅回答",
            "instructions": "不调用工具",
            "allowed_tools": [],
        },
        headers=headers,
    ).json()
    headers["Idempotency-Key"] = str(uuid4())
    result = client.post(
        "/api/v1/runs", json={**body, "skill_version_id": skill["version_id"]}, headers=headers
    )
    assert result.status_code == 202, result.text
    with Session(client.app.state.database) as db:
        assert db.get(Run, result.json()["id"]).snapshot["tool_catalog"] == {}


def mock_http(
    monkeypatch: Any, payload: object, status: int = 200, content_type: str = "application/json"
) -> list[dict[str, Any]]:
    """固定公共 DNS 与内存 HTTP 响应，收集请求但绝不连接外部服务。"""
    records: list[dict[str, Any]] = []
    encoded = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    class Response:
        """提供有界分块读取以测试返回格式和容量检查。"""

        def __init__(self) -> None:
            """初始化当前响应的读取位置。"""
            self.status = status
            self.position = 0

        def getheader(self, name: str, default: str = "") -> str:
            """仅声明模拟 JSON 类型，其他响应头沿用默认值。"""
            return content_type if name == "Content-Type" else default

        def read1(self, limit: int) -> bytes:
            """按调用方容量上限返回字节，保留流式容量检验路径。"""
            chunk = encoded[self.position : self.position + limit]
            self.position += len(chunk)
            return chunk

    class Connection:
        """替换网络连接，不创建套接字或请求真实接口。"""

        sock = None

        def __init__(self, host: str, address: str, timeout: float) -> None:
            """记录经过安全策略验证的域名、固定地址和期限。"""
            records.append({"host": host, "address": address, "timeout": timeout})

        def request(
            self, method: str, target: str, headers: dict[str, str], body: bytes | None = None
        ) -> None:
            """收集实际请求参数，验证秘密仅在传输时注入。"""
            records[-1].update({"method": method, "target": target, "headers": headers, "body": body})

        def getresponse(self) -> Response:
            """返回内存 JSON 响应。"""
            return Response()

        def close(self) -> None:
            """模拟资源关闭，不涉及外部状态。"""
            pass

    monkeypatch.setattr(http_runtime, "resolve_public", lambda *args: "8.8.8.8")
    monkeypatch.setattr(http_runtime, "PinnedHTTPSConnection", Connection)
    return records


@pytest.mark.parametrize(
    "name,args",
    [
        ("weather_query", {"city": "110101", "extensions": "all"}),
        ("traffic_query", {"name": "北环大道", "adcode": "440300"}),
    ],
)
def test_amap_key_only_in_transport(monkeypatch: Any, name: str, args: dict[str, str]) -> None:
    """天气和路况使用固定端点，密钥不进入模型输入，服务返回真实来源标识。"""
    config = default_config(name)
    settings = Settings(
        _env_file=None,
        tool_http_allowed_hosts=["restapi.amap.com"],
        amap_api_key="test-secret-unique",
    )
    records = mock_http(
        monkeypatch,
        {"status": "1", "forecasts": [{"reporttime": "fixture-time", "casts": []}]}
        if name == "weather_query"
        else {"status": "1", "trafficinfo": {"description": "畅通"}},
    )
    pin = {"builtin": True, "config": config}
    result = execute_http(pin, args, settings, lambda: None)
    query = parse_qs(urlsplit(records[0]["target"]).query)
    assert query["key"] == ["test-secret-unique"] and query["output"] == ["JSON"]
    assert "Authorization" not in records[0]["headers"]
    assert result["ok"] and result["source"] == "高德地图"
    assert "test-secret-unique" not in json.dumps({"pin": pin, "args": args, "result": result})
    mock_http(monkeypatch, {"status": "0", "info": "INVALID_USER_KEY"})
    assert execute_http(pin, args, settings, lambda: None) == {
        "ok": False,
        "error_code": "tool_provider_rejected",
    }
    mock_http(monkeypatch, {"status": "1", "lives": [], "forecasts": [], "trafficinfo": {}})
    assert execute_http(pin, args, settings, lambda: None)["error_code"] == "tool_no_data"


@pytest.mark.parametrize(
    "payload,status,ctype,code",
    [
        ({"ok": 1}, 302, "application/json", "tool_http_failed"),
        (b"<html>bad</html>", 200, "text/html", "tool_response_invalid"),
        (b"not-json", 200, "application/json", "tool_response_invalid"),
        ({"data": "x" * 9000}, 200, "application/json", "tool_result_too_large"),
        ({"data": "test-secret-unique"}, 200, "application/json", "tool_result_sensitive"),
    ],
)
def test_remote_response_is_bounded(
    monkeypatch: Any, payload: object, status: int, ctype: str, code: str
) -> None:
    """重定向、非 JSON、过大响应和回显密钥不进入模型上下文。"""
    config = ToolInput.model_validate(tool_data(secret_name="LOOKUP")).model_dump(
        exclude={"enabled"}
    )
    settings = Settings(
        _env_file=None,
        tool_http_allowed_hosts=["api.example.com"],
        tool_secrets={"LOOKUP": "test-secret-unique"},
    )
    mock_http(monkeypatch, payload, status, ctype)
    assert (
        execute_http({"config": config}, {"q": "查询"}, settings, lambda: None)["error_code"]
        == code
    )


def test_dns_mixed_private_is_denied(monkeypatch: Any) -> None:
    """即使首个 DNS 结果为公网，只要同时解析到内网也拒绝实际连接。"""
    monkeypatch.setattr(
        http_runtime.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("8.8.8.8", 443)),
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ],
    )
    with pytest.raises(ProviderFailure, match="tool_network_denied"):
        resolve_public("api.example.com", time.monotonic() + 3)


def test_missing_secret_and_cancel_prevent_connection(monkeypatch: Any) -> None:
    """未配置或取消时在网络调用前拒绝，不返回成功样例。"""
    records = mock_http(monkeypatch, {"status": "1"})
    pin = {"builtin": True, "config": default_config("weather_query")}
    settings = Settings(_env_file=None, tool_http_allowed_hosts=["restapi.amap.com"])
    with pytest.raises(ProviderFailure, match="tool_not_configured"):
        execute_http(pin, {"city": "110101"}, settings, lambda: None)
    settings = Settings(
        _env_file=None, tool_http_allowed_hosts=["restapi.amap.com"], amap_api_key="fixture"
    )

    def cancelled() -> None:
        """模拟用户撤销，执行器须在网络请求前停止。"""
        raise ProviderFailure("cancelled")

    with pytest.raises(ProviderFailure, match="cancelled"):
        execute_http(pin, {"city": "110101"}, settings, cancelled)
    assert records == []


@pytest.mark.parametrize("revoke", [False, True])
def test_http_tool_executes_with_frozen_schema_and_live_revoke(
    client: Any, monkeypatch: Any, revoke: bool
) -> None:
    """生产执行器向模型提供冻结 Schema，并在网络结果提交前复核实时停用。"""
    space, _, headers = context(client)
    settings = client.app.state.settings
    settings.tool_http_allowed_hosts = ["api.example.com"]
    settings.task_budget_enabled = False
    settings.cumulative_budget_enabled = False
    path = f"/api/v1/me/tools"
    client.post(path, json=tool_data(), headers=headers)
    headers, _, task, _ = setup_task(client, tools=["public_lookup"])
    recorded = mock_http(monkeypatch, {"records": ["公开资料"]})

    class Provider(HTTPProvider):
        """使用真实协议适配器结构，仅替换流响应以免产生付费调用。"""

        def __init__(self) -> None:
            """保存离线测试状态，继承生产 Schema 组装入口。"""
            super().__init__(settings, "openai_compatible", "fixture-model")
            self.calls = 0

        def stream(
            self, messages: list[dict[str, Any]], tools: list[str], max_tokens: int
        ) -> Iterator[ModelEvent]:
            """验证每轮请求冻结的查询契约，返回工具提案和成功答复。"""
            self.guard()
            body = self.request_body(messages, tools, max_tokens)
            assert body["tools"][0]["function"]["name"] == "public_lookup"
            assert body["tools"][0]["function"]["parameters"]["required"] == ["q"]
            self.calls += 1
            if self.calls == 1:
                yield ModelEvent(
                    "reply",
                    reply=Reply(
                        calls=[ToolCall(id="query", name="public_lookup", arguments={"q": "资料"})]
                    ),
                )
            else:
                yield ModelEvent("text", text="查询完成")
                yield ModelEvent("reply", reply=Reply())

    provider = Provider()
    if revoke:
        original = http_runtime.execute_http

        def revoke_in_network(
            pin: dict[str, Any], arguments: object, current_settings: Settings, guard: Any
        ) -> dict[str, object]:
            """模拟请求已发出后管理员停用，查询返回不能写入成功缓存。"""
            result = original(pin, arguments, current_settings, guard)
            response = client.patch(
                path + "/public_lookup", json={"enabled": False}, headers=headers
            )
            assert response.status_code == 200
            return result

        monkeypatch.setattr(http_runtime, "execute_http", revoke_in_network)
    Execution(client.app.state.database, settings, task["id"]).execute(lambda *args: provider)
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        item = db.scalar(select(ToolExecution).where(ToolExecution.run_id == run.id))
        assert run.status == ("failed" if revoke else "succeeded")
        if revoke:
            assert run.error_code == "tool_unavailable" and item.status != "succeeded"
        else:
            assert item.result == {"ok": True, "data": {"records": ["公开资料"]}}
    assert len(recorded) == 1


def test_amap_invalid_parameters_before_network(monkeypatch: Any) -> None:
    """无效编码或天气类型不能被模型任意构造后交给外部服务。"""
    records = mock_http(monkeypatch, {"status": "1"})
    settings = Settings(
        _env_file=None, tool_http_allowed_hosts=["restapi.amap.com"], amap_api_key="fixture"
    )
    pin = {"builtin": True, "config": default_config("weather_query")}
    for arguments in [
        {"city": "北京"},
        {"city": "110101", "extensions": "unknown"},
        {"city": "110101", "key": "injected"},
    ]:
        with pytest.raises(ValueError):
            execute_http(pin, arguments, settings, lambda: None)
    assert records == []


def test_pinned_connection_tls_identity_and_shared_deadline(monkeypatch: Any) -> None:
    """连接只用已验证 IP，TLS 验原域名，TCP 与 TLS 共享同一截止时间。"""
    clock = iter([100.0, 101.0, 103.0])
    monkeypatch.setattr(http_runtime.time, "monotonic", lambda: next(clock))
    raw = Mock()
    connect = Mock(return_value=raw)
    monkeypatch.setattr(http_runtime.socket, "create_connection", connect)
    connection = http_runtime.PinnedHTTPSConnection("api.example.com", "8.8.8.8", 5)
    assert connection.tls_context.check_hostname
    assert connection.tls_context.verify_mode == http_runtime.ssl.CERT_REQUIRED
    wrap = Mock(return_value=raw)
    monkeypatch.setattr(connection.tls_context, "wrap_socket", wrap)
    connection.connect()
    connect.assert_called_once_with(("8.8.8.8", 443), 4.0)
    raw.settimeout.assert_called_once_with(2.0)
    wrap.assert_called_once_with(raw, server_hostname="api.example.com")
    monkeypatch.setattr(http_runtime.time, "monotonic", lambda: 106.0)
    with pytest.raises(TimeoutError):
        connection.connect()
    assert connect.call_count == 1

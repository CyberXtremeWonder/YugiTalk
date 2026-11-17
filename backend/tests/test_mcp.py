"""MCP 网络协议、用户隔离、撤权及执行恢复；无真实外部调用。"""

import copy
import io
import json
from typing import Any
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session
from test_identity import ORIGIN, login

from app.providers.client import ProviderFailure, ToolCall
from app.tasks.models import Run
from app.tasks.reliability import prepare_bound_tool
from app.tools.mcp import client as mcp_client
from app.tools.mcp import router as mcp_router
from app.tools.mcp import service as mcp_service
from app.tools.mcp.models import MCPCredential
from app.tools.service import bound_tool, freeze_tools

pytest_plugins = ["test_identity"]
PATH = "/api/v1/me/mcp-servers"
TOOL = {
    "name": "lookup",
    "description": "查询记录",
    "inputSchema": {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
        "additionalProperties": False,
    },
}


class Response(io.BytesIO):
    def __init__(self, body: str, content_type: str = "application/json", status: int = 200):
        super().__init__(body.encode())
        self.status = status
        self.headers = {"Content-Type": content_type}

    def getheader(self, name: str, default: Any = None) -> Any:
        return self.headers.get(name, default)


@pytest.mark.parametrize("transport", ["sse", "streamable_http"])
def test_wire_protocol(monkeypatch: Any, transport: str) -> None:
    requests = []
    responses = []
    sequence = [
        (1, {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}}),
        (3, {"tools": [TOOL]}),
        (4, {"content": [{"type": "text", "text": "ok"}]}),
    ]
    if transport == "sse":
        body = "event: endpoint\ndata: /messages?sessionId=private\n\n"
        for key, result in sequence:
            body += (
                "event: message\ndata: "
                + json.dumps({"jsonrpc": "2.0", "id": key, "result": result})
                + "\n\n"
            )
        responses.append(Response(body, "text/event-stream"))
        responses += [Response("", status=202) for _ in range(4)]
    else:
        for key, result in sequence:
            body = json.dumps({"jsonrpc": "2.0", "id": key, "result": result})
            response = Response(
                body if key != 4 else f"data: {body}\n\n",
                "application/json" if key != 4 else "text/event-stream",
            )
            responses.append(response)
            if key == 1:
                responses.append(Response("", status=202))
    connections = []

    class Connection:
        def __init__(self, host: str, address: str, timeout: float):
            assert host == "api.example.com" and address == "8.8.8.8"
            self.closed = False
            connections.append(self)

        def request(self, method: str, path: str, **kwargs: Any) -> None:
            requests.append((method, path, kwargs))

        def getresponse(self) -> Response:
            return responses.pop(0)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(mcp_client, "PinnedHTTPSConnection", Connection)
    monkeypatch.setattr(mcp_client, "resolve_public", lambda *args: "8.8.8.8")
    with mcp_client.MCPClient(
        {"url": "https://api.example.com/mcp", "transport": transport}, "secret"
    ) as client:
        assert client.tools() == [TOOL]
        assert (
            client.rpc("tools/call", {"name": "lookup", "arguments": {"q": "hello"}})["content"][0][
                "text"
            ]
            == "ok"
        )
    assert all(c.closed for c in connections)
    assert all(r[2]["headers"]["Authorization"] == "Bearer secret" for r in requests)
    if transport == "sse":
        assert requests[1][1] == "/messages?sessionId=private"


def test_endpoint_rejection(monkeypatch: Any) -> None:
    resolve = Mock()
    monkeypatch.setattr(mcp_client, "resolve_public", resolve)
    client = mcp_client.MCPClient(
        {"url": "https://api.example.com/sse", "transport": "sse"}, "private"
    )
    for url in [
        "https://evil.example.com/post",
        "http://api.example.com/post",
        "https://api.example.com:444/post",
        "https://x@api.example.com/post",
    ]:
        with pytest.raises(ProviderFailure, match="mcp_endpoint_denied"):
            client.request("POST", url, {})
    resolve.assert_not_called()


def create(client: Any, monkeypatch: Any) -> tuple[Any, Any, Any, Any]:
    identity = login(client, "member@example.test")
    headers = {**ORIGIN, "X-CSRF-Token": identity["csrf_token"]}
    fake = Mock()
    fake.__enter__ = Mock(return_value=fake)
    fake.__exit__ = Mock(return_value=None)
    fake.tools.return_value = [copy.deepcopy(TOOL)]
    fake.rpc.return_value = {"content": [{"type": "text", "text": "ok"}]}
    factory = Mock(return_value=fake)
    monkeypatch.setattr(mcp_router, "MCPClient", factory)
    monkeypatch.setattr(mcp_service, "MCPClient", factory)
    response = client.post(
        PATH,
        json={
            "name": "测试",
            "url": "https://api.example.com/mcp",
            "transport": "streamable_http",
            "token": "private-token",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    server = response.json()
    assert "private-token" not in response.text and server["has_token"]
    response = client.post(f"{PATH}/{server['id']}/discover", headers=headers)
    assert response.status_code == 200, response.text
    assert not response.json()["tools"][0]["enabled"]
    name = response.json()["tools"][0]["name"]
    assert (
        client.patch(
            f"/api/v1/me/tools/{name}", json={"enabled": True}, headers=headers
        ).status_code
        == 200
    )
    return identity, headers, server, fake


def test_crud_isolation_and_revocation(client: Any, monkeypatch: Any) -> None:
    identity, headers, server, fake = create(client, monkeypatch)
    user = identity["user"]["id"]
    catalog = client.get("/api/v1/me/tools").json()["items"]
    name = next(t["name"] for t in catalog if t["kind"] == "mcp")
    assert fake.rpc.call_count == 0  # 发现操作绝不执行业务工具。
    with Session(client.app.state.database) as db:
        secret = db.get(MCPCredential, server["id"])
        assert "private-token" not in secret.encrypted
        pins = freeze_tools(db, user, user, [name], client.app.state.settings)
        assert "private-token" not in json.dumps(pins)
    other = login(client, "other@example.test")
    other_headers = {**ORIGIN, "X-CSRF-Token": other["csrf_token"]}
    assert client.get(PATH).json()["items"] == []
    assert not any(t["name"] == name for t in client.get("/api/v1/me/tools").json()["items"])
    for method, suffix, body in [
        ("post", "/discover", {}),
        ("patch", "", {"enabled": False}),
        ("delete", "", None),
    ]:
        response = getattr(client, method)(
            f"{PATH}/{server['id']}{suffix}",
            headers=other_headers,
            **({"json": body} if body is not None else {}),
        )
        assert response.status_code == 404
    identity = login(client, "member@example.test")
    headers = {**ORIGIN, "X-CSRF-Token": identity["csrf_token"]}
    result = mcp_service.execute(
        client.app.state.database,
        user,
        pins[name],
        {"q": "hello"},
        client.app.state.settings,
        lambda: None,
    )
    assert result["ok"] and fake.rpc.call_count == 1
    with pytest.raises(ProviderFailure, match="mcp_arguments_invalid"):
        mcp_service.execute(
            client.app.state.database,
            user,
            pins[name],
            {"q": 123},
            client.app.state.settings,
            lambda: None,
        )
    assert (
        client.patch(f"{PATH}/{server['id']}", json={"enabled": False}, headers=headers).status_code
        == 200
    )
    with Session(client.app.state.database) as db:
        with pytest.raises(ProviderFailure, match="tool_unavailable"):
            bound_tool(db, user, user, {"tool_catalog": pins}, name)
    assert client.delete(f"{PATH}/{server['id']}", headers=headers).status_code == 204
    with Session(client.app.state.database) as db:
        assert db.get(MCPCredential, server["id"]) is None


def test_revision_and_uncertain_execution(client: Any, monkeypatch: Any) -> None:
    identity, headers, server, _ = create(client, monkeypatch)
    user = identity["user"]["id"]
    with Session(client.app.state.database) as db:
        name = next(
            t["name"] for t in client.get("/api/v1/me/tools").json()["items"] if t["kind"] == "mcp"
        )
        pins = freeze_tools(db, user, user, [name], client.app.state.settings)
        run = Run(
            id="test-run",
            account_id=user,
            owner_id=user,
            snapshot={"tools": [name], "tool_catalog": pins},
        )
        call = ToolCall(id="call-1", name=name, arguments={"q": "hello"})
        item, _ = prepare_bound_tool(db, run, call, 1, client.app.state.settings)
        db.commit()
        with pytest.raises(ProviderFailure, match="mcp_execution_uncertain"):
            prepare_bound_tool(db, run, call, 1, client.app.state.settings)
        prepare_bound_tool(db, run, call, 1, client.app.state.settings, completing=True)
    response = client.put(
        f"{PATH}/{server['id']}",
        json={"name": "新版", "url": server["url"], "transport": "sse"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    with Session(client.app.state.database) as db:
        with pytest.raises(ProviderFailure, match="tool_unavailable"):
            bound_tool(db, user, user, {"tool_catalog": pins}, name)


@pytest.mark.parametrize(
    "url",
    [
        "http://public.example.com/sse",
        "https://127.0.0.1/sse",
        "https://169.254.169.254/",
        "https://api.example.com/sse?key=secret",
    ],
)
def test_private_endpoints_denied(client: Any, url: str) -> None:
    identity = login(client)
    assert (
        client.post(
            PATH,
            json={"name": "bad", "url": url, "transport": "sse"},
            headers={**ORIGIN, "X-CSRF-Token": identity["csrf_token"]},
        ).status_code
        == 422
    )


def test_remote_schema_rejected() -> None:
    bad = copy.deepcopy(TOOL)
    bad["inputSchema"]["properties"]["q"] = {"$ref": "https://evil.example.com/schema"}
    with pytest.raises(ValueError, match="外部"):
        mcp_service.checked_tools([bad])


@pytest.mark.parametrize("code", ["mcp_transport_mismatch", "mcp_auth_required", "mcp_timeout"])
def test_discovery_explains_failures(client: Any, monkeypatch: Any, code: str) -> None:
    _, headers, server, fake = create(client, monkeypatch)
    fake.tools.side_effect = ProviderFailure(code)
    response = client.post(f"{PATH}/{server['id']}/discover", headers=headers)
    assert response.status_code == 422
    assert response.json()["code"] == code
    assert len(response.json()["message"]) > 20
    assert "private-token" not in response.text


def test_migration_matches_models() -> None:
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    from app.tools.mcp.models import MCPCredential, MCPServer, MCPServerVersion

    migration = importlib.import_module("migrations.versions.0021_mcp")
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            inspector = inspect(connection)
            for model in (MCPServer, MCPServerVersion, MCPCredential):
                table = model.__table__
                columns = inspector.get_columns(table.name)
                assert {c["name"] for c in columns} == set(table.c.keys())
                assert {c["name"]: c["nullable"] for c in columns} == {
                    c.name: c.nullable for c in table.c
                }
                assert len(inspector.get_foreign_keys(table.name)) == 1
            migration.downgrade()
            assert not inspect(connection).get_table_names()
    engine.dispose()


def test_discovery_with_builtin_override(client: Any, monkeypatch: Any) -> None:
    identity, headers, server, _ = create(client, monkeypatch)
    assert (
        client.patch(
            "/api/v1/me/tools/text_stats", json={"enabled": False}, headers=headers
        ).status_code
        == 200
    )
    assert client.get(PATH).status_code == 200
    assert client.post(f"{PATH}/{server['id']}/discover", headers=headers).status_code == 200
    assert client.delete(f"{PATH}/{server['id']}", headers=headers).status_code == 204


def test_oversized_response_and_redirect_denied(monkeypatch: Any) -> None:
    client = mcp_client.MCPClient(
        {"url": "https://api.example.com/mcp", "transport": "streamable_http"}
    )
    with pytest.raises(ProviderFailure, match="mcp_response_too_large"):
        list(client.chunks(Response("x" * (mcp_client.LIMIT + 1))))
    monkeypatch.setattr(mcp_client, "resolve_public", lambda *args: "8.8.8.8")
    connection = Mock()
    connection.getresponse.return_value = Response("", status=302)
    monkeypatch.setattr(mcp_client, "PinnedHTTPSConnection", lambda *args: connection)
    client = mcp_client.MCPClient(
        {"url": "https://api.example.com/mcp", "transport": "streamable_http"}
    )
    with pytest.raises(ProviderFailure, match="mcp_http_failed"):
        with client:
            pass
    assert connection.request.call_count == 1
    connection.close.assert_called_once()


@pytest.mark.parametrize("use_mcp", [False, True])
def test_subagent_decides_mcp(client: Any, monkeypatch: Any, use_mcp: bool) -> None:
    from test_subagents import Script, execute, tool

    from app.tasks.ledger_models import ToolExecution

    identity, headers, server, fake = create(client, monkeypatch)
    name = client.get(PATH).json()["items"][0]["tools"][0]["name"]
    agent = client.post(
        "/api/v1/me/agents",
        json={
            "name": "MCP 助手",
            "tools": [name],
            "token_budget": 100000,
        },
        headers=headers,
    )
    assert agent.status_code == 201, agent.text
    conversation = client.post(
        "/api/v1/me/conversations", json={"title": "测试"}, headers=headers
    ).json()
    client.app.state.dispatch_run = lambda key: None
    response = client.post(
        "/api/v1/runs",
        json={
            "conversation_id": conversation["id"],
            "agent_version_id": agent.json()["version_id"],
            "message": "你好",
        },
        headers={**headers, "Idempotency-Key": "test-mcp-routing"},
    )
    assert response.status_code == 202, response.text
    task = response.json()
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        assert run.snapshot["router_mode"] and not run.snapshot["direct_mode"]
    worker = [tool(name, q="hello"), "子助手依据工具回答"] if use_mcp else ["无需调用 MCP"]

    def verify_origin(messages: Any) -> str:
        result = json.loads(messages[-1]["content"])
        assert result["tool_usage"] == (
            [
                {
                    "name": name,
                    "source": "mcp",
                    "remote_name": "lookup",
                    "status": "succeeded",
                }
            ]
            if use_mcp
            else []
        )
        return "最终答案"

    result = execute(
        client,
        task,
        [
            Script([tool("delegate_task", task="处理用户问题"), verify_origin], ["delegate_task"]),
            Script(worker, [
                name, "list_knowledge_bases", "knowledge_search",
                "research_report", "export_document",
            ]),
        ],
    )
    assert result["status"] == "succeeded", result
    assert fake.rpc.call_count == int(use_mcp)
    from sqlalchemy import select

    with Session(client.app.state.database) as db:
        rows = db.scalars(select(ToolExecution).where(ToolExecution.run_id == task["id"])).all()
        assert len(rows) == int(use_mcp)
        if rows:
            assert rows[0].status == "succeeded"

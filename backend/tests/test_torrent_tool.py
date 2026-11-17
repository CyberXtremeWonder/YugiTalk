"""BT 工具的 RPC 契约、权限隔离及执行恢复；不连接真实 swarm。"""

import base64
import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_agents import setup_task
from test_subagents import Script, tool

from app.core.config import Settings
from app.providers.client import ProviderFailure, ToolCall
from app.tasks.execution import Execution
from app.tasks.ledger_models import ToolExecution
from app.tools.http import pictures, torrent
from app.tools.http.torrent_contract import DownloadTorrent

pytest_plugins = ["test_identity"]
MAGNET = "magnet:?xt=urn:btih:" + "a" * 40


@pytest.fixture
def settings():
    return Settings(
        _env_file=None, aria2_rpc_url="http://127.0.0.1:6800/jsonrpc",
        aria2_rpc_secret=SecretStr("test-rpc-secret"), torrent_download_root="/downloads",
    )


@pytest.mark.parametrize("arguments", [
    {"action": "start"}, {"action": "status", "source": MAGNET},
    {"action": "start", "source": MAGNET, "download_id": "x"},
    {"action": "start", "source": MAGNET, "dir": "/tmp"},
])
def test_arguments_reject_ambiguous_or_server_options(arguments):
    with pytest.raises(ValidationError):
        DownloadTorrent.model_validate(arguments)


def test_start_torrent_and_magnet_use_fixed_options(settings, monkeypatch):
    calls = []

    def rpc(config, method, params):
        calls.append((method, params))
        return params[-1]["gid"]

    monkeypatch.setattr(torrent, "rpc", rpc)
    content = b"d4:infodee"
    monkeypatch.setattr(pictures, "read_url", lambda *a, **kw: (content, "", a[0]))
    first = torrent.download_torrent(
        {"action": "start", "source": MAGNET}, settings, "alice", "call-1", lambda: None,
    )
    second = torrent.download_torrent(
        {"action": "start", "source": "https://example.test/file.torrent"},
        settings, "alice", "call-2", lambda: None,
    )
    assert first["status"] == second["status"] == "submitted"
    assert calls[0][0] == "addUri"
    assert calls[1][0] == "addTorrent"
    assert base64.b64decode(calls[1][1][0]) == content
    assert calls[0][1][-1]["seed-time"] == "0"
    assert calls[0][1][-1]["dir"].startswith("/downloads/")
    assert calls[0][1][-1]["dir"] != calls[1][1][-1]["dir"]
    assert "test-rpc-secret" not in json.dumps(first)


def test_status_follows_magnet_metadata_and_denies_other_user(settings, monkeypatch):
    monkeypatch.setattr(torrent, "rpc", lambda s, m, p: p[-1]["gid"])
    submitted = torrent.download_torrent(
        {"action": "start", "source": MAGNET}, settings, "alice", "call-1", lambda: None,
    )
    calls = []

    def rpc(s, method, params):
        calls.append(params[0])
        return {
            "dir": submitted["save_directory"], "status": "active", "totalLength": "100",
            "completedLength": "25", "downloadSpeed": "10",
            **({"followedBy": ["b" * 16]} if len(calls) == 1 else {}),
        }

    monkeypatch.setattr(torrent, "rpc", rpc)
    args = {"action": "status", "download_id": submitted["download_id"]}
    denied = torrent.download_torrent(args, settings, "bob", "new", lambda: None)
    assert denied["error_code"] == "tool_not_authorized"
    assert not calls
    result = torrent.download_torrent(args, settings, "alice", "new", lambda: None)
    assert result["progress_percent"] == 25
    assert result["download_id"] == submitted["download_id"]
    assert calls[-1] == "b" * 16


@pytest.mark.parametrize("source", ["file:///etc/passwd", "http://example.test/x", "magnet:?xt=x"])
def test_bad_sources_never_reach_rpc(settings, monkeypatch, source):
    monkeypatch.setattr(torrent, "rpc", lambda *a: pytest.fail("unexpected RPC"))
    result = torrent.download_torrent(
        {"action": "start", "source": source}, settings, "alice", "call", lambda: None,
    )
    assert not result["ok"]


def test_rpc_auth_and_bounded_response(settings, monkeypatch):
    original = httpx.Client
    responses = iter([{"jsonrpc": "2.0", "id": "torrent", "result": "gid"},
                      {"id": "torrent", "error": {"message": "secret"}}, "x" * 70000])

    def handler(request):
        assert json.loads(request.content)["params"][0] == "token:test-rpc-secret"
        return httpx.Response(200, json=next(responses))

    monkeypatch.setattr(torrent.httpx, "Client", lambda **kw: original(
        transport=httpx.MockTransport(handler), **kw,
    ))
    assert torrent.rpc(settings, "addUri", []) == "gid"
    with pytest.raises(ProviderFailure, match="tool_provider_rejected"):
        torrent.rpc(settings, "addUri", [])
    with pytest.raises(ProviderFailure, match="tool_result_too_large"):
        torrent.rpc(settings, "tellStatus", [])


def test_rpc_timeout_is_sanitized(settings, monkeypatch):
    original = httpx.Client

    def handler(request):
        raise httpx.ReadTimeout("test-rpc-secret", request=request)

    monkeypatch.setattr(torrent.httpx, "Client", lambda **kw: original(
        transport=httpx.MockTransport(handler), **kw,
    ))
    with pytest.raises(ProviderFailure, match="^tool_timeout$"):
        torrent.rpc(settings, "addUri", [])


def test_private_torrent_url_does_not_reach_rpc(settings, monkeypatch):
    monkeypatch.setattr(torrent, "rpc", lambda *a: pytest.fail("unexpected RPC"))
    result = torrent.download_torrent(
        {"action": "start", "source": "https://127.0.0.1/private.torrent"},
        settings, "alice", "call", lambda: None,
    )
    assert result["error_code"] == "tool_network_denied"


def test_uncertain_submit_keeps_same_id_and_cancellation_propagates(settings, monkeypatch):
    def fail(*args):
        raise ProviderFailure("tool_timeout")

    monkeypatch.setattr(torrent, "rpc", fail)
    args = {"action": "start", "source": MAGNET}
    first = torrent.download_torrent(args, settings, "alice", "call", lambda: None)
    second = torrent.download_torrent(args, settings, "alice", "call", lambda: None)
    assert not first["ok"] and first["download_id"] == second["download_id"]

    def cancel():
        raise ProviderFailure("cancelled")

    with pytest.raises(ProviderFailure, match="cancelled"):
        torrent.download_torrent(args, settings, "alice", "call", cancel)


def test_agent_registration_readiness_and_ledger_reuse(client, monkeypatch):
    items = client.get("/api/v1/me/tools")
    assert items.status_code == 401
    config = client.app.state.settings
    config.aria2_rpc_url = "http://127.0.0.1:6800/jsonrpc"
    config.aria2_rpc_secret = SecretStr("test-secret")
    _, _, task, _ = setup_task(client, "下载这个磁力链接", tools=[torrent.TOOL_NAME])
    item = next(i for i in client.get("/api/v1/me/tools").json()["items"]
                if i["name"] == torrent.TOOL_NAME)
    assert item["ready"]
    calls = []

    def rpc(s, method, params):
        calls.append(method)
        return params[-1]["gid"]

    monkeypatch.setattr(torrent, "rpc", rpc)
    execution = Execution(client.app.state.database, config, task["id"])
    arguments = {"action": "start", "source": MAGNET}

    def confirm(messages):
        with Session(client.app.state.database) as db:
            ledger = db.scalar(select(ToolExecution))
            step, call_id = ledger.call_key.split(":", 1)
            cached = dict(ledger.result)
        assert execution.tool(ToolCall(id=call_id, name=torrent.TOOL_NAME, arguments=arguments),
                              int(step)) == cached
        assert calls == ["addUri"]
        return "已提交后台下载。"

    provider = Script(
        [tool(torrent.TOOL_NAME, **arguments), confirm],
        [torrent.TOOL_NAME, "list_knowledge_bases", "knowledge_search", "research_report",
         "export_document"],
    )
    execution.execute(lambda *args: provider)
    assert client.get(f"/api/v1/runs/{task['id']}").json()["status"] == "succeeded"
    config.aria2_rpc_url = ""
    item = next(i for i in client.get("/api/v1/me/tools").json()["items"]
                if i["name"] == torrent.TOOL_NAME)
    assert not item["ready"] and "ARIA2_RPC_URL" in item["configuration_note"]

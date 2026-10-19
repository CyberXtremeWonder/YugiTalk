"""Tavily 默认工具的授权、协议、容量和脱敏回归，不请求真实服务。"""

import json

import pytest
from app.agents.contracts import AgentConfig
from app.core.config import Settings
from app.providers.client import ProviderFailure, Reply, ToolCall
from app.tasks.execution import Execution
from app.tasks.ledger_models import ToolExecution
from app.tasks.models import Run
from app.tools.http_runtime import execute_http
from app.tools.management_contracts import ToolInput
from app.tools.service import bound_tool, secret_value
from app.tools.web_search import WebSearch, search_config, search_result
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_agent_graph import ScriptedProvider
from test_agents import setup_task
from test_identity import ORIGIN, login
from test_tools_management import mock_http, tool_data

pytest_plugins = ["test_identity"]


def settings():
    """生成仅包含合成密钥的隔离配置。"""
    return Settings(
        _env_file=None,
        tool_http_allowed_hosts=["api.tavily.com"],
        tavily_api_key="fixture-tavily-secret",
    )


def pin():
    """构造固定服务声明，不携带实际密钥。"""
    return {"builtin": True, "config": search_config()}


def payload(content="公开资料", url="https://example.com/page"):
    """生成带来源与正文的模拟搜索响应。"""
    return {
        "results": [
            {"title": "资料标题", "url": url, "content": content, "raw_content": "网页正文"}
        ]
    }


def test_post_protocol_and_key_is_transport_only(monkeypatch):
    """验证固定 POST、默认搜索选项和密钥不进入工具结果。"""
    records = mock_http(monkeypatch, payload())
    result = execute_http(pin(), {"query": "公开资料"}, settings(), lambda: None)
    request = records[0]
    assert request["host"] == "api.tavily.com"
    assert request["method"] == "POST" and request["target"] == "/search"
    assert request["headers"]["Authorization"] == "Bearer fixture-tavily-secret"
    body = json.loads(request["body"])
    assert body["query"] == "公开资料" and body["max_results"] == 3
    assert body["include_raw_content"] == "text" and body["search_depth"] == "basic"
    assert body["include_answer"] is False and body["auto_parameters"] is False
    assert result["ok"] and result["source"] == "Tavily"
    assert result["results"][0]["raw_content"] == "网页正文"
    assert "fixture-tavily-secret" not in json.dumps([pin(), body, result])
    assert (
        secret_value({**search_config(), "endpoint": "https://other.com/search"}, settings()) == ""
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": ""},
        {"query": "  "},
        {"query": "a" * 501},
        {"query": "测试", "max_results": 0},
        {"query": "测试", "max_results": 6},
        {"query": "测试", "max_results": True},
        {"query": "测试", "api_key": "secret"},
        {"query": "测试", "url": "http://127.0.0.1"},
    ],
)
def test_invalid_arguments_never_send_request(monkeypatch, arguments):
    """无效参数在 DNS 和网络请求前被拒绝。"""
    records = mock_http(monkeypatch, payload())
    with pytest.raises(ValidationError):
        execute_http(pin(), arguments, settings(), lambda: None)
    assert not records


@pytest.mark.parametrize("key,hosts", [("", ["api.tavily.com"]), ("fixture", [])])
def test_missing_configuration_never_requests(monkeypatch, key, hosts):
    """密钥或白名单未配置时明确拒绝，不发送匿名或替代查询。"""
    records = mock_http(monkeypatch, payload())
    config = Settings(_env_file=None, tavily_api_key=key, tool_http_allowed_hosts=hosts)
    with pytest.raises(ProviderFailure, match="tool_not_configured"):
        execute_http(pin(), {"query": "测试"}, config, lambda: None)
    assert not records


@pytest.mark.parametrize(
    "response,status,ctype,error",
    [
        (payload(), 401, "application/json", "tool_http_failed"),
        (payload(), 429, "application/json", "tool_http_failed"),
        (payload(), 302, "application/json", "tool_http_failed"),
        (b"not-json", 200, "application/json", "tool_response_invalid"),
        (b"<html>error</html>", 200, "text/html", "tool_response_invalid"),
        ({"results": "invalid"}, 200, "application/json", "tool_response_invalid"),
        (payload("fixture-tavily-secret"), 200, "application/json", "tool_result_sensitive"),
        (payload("a" * (2 * 1024 * 1024)), 200, "application/json", "tool_result_too_large"),
    ],
)
def test_service_failures_are_bounded(monkeypatch, response, status, ctype, error):
    """服务故障、重定向、过大或泄密结果不作为网页证据返回。"""
    mock_http(monkeypatch, response, status, ctype)
    assert execute_http(pin(), {"query": "测试"}, settings(), lambda: None) == {
        "ok": False,
        "error_code": error,
    }


def test_raw_page_text_is_clipped_and_empty_search_is_valid(monkeypatch):
    """大正文仅返回有界摘录，正常零结果不伪装为服务故障。"""
    data = payload("资料" * 10000)
    data["results"][0]["raw_content"] = "正文" * 10000
    mock_http(monkeypatch, data)
    result = execute_http(pin(), {"query": "测试"}, settings(), lambda: None)
    assert result["ok"] and len(json.dumps(result, ensure_ascii=False).encode()) < 16000
    assert len(result["results"][0]["raw_content"].encode()) <= 1300
    assert search_result({"results": []}, 3) == {"ok": True, "source": "Tavily", "results": []}


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "http://127.0.0.1/page",
        "http://[::1]/",
        "http://10.0.0.1/",
        "https://user:pass@example.com/",
        "http://localhost/",
        "https://example.com:8000/",
    ],
)
def test_unsafe_source_identifiers_are_discarded(url):
    """危险来源不能作为可引用的网页结果返回，也不会触发二次抓取。"""
    assert search_result(payload(url=url), 3)["results"] == []


def test_custom_tools_still_cannot_use_arbitrary_post():
    """固定 Tavily 例外不放宽管理员声明工具的任意 POST 权限。"""
    with pytest.raises(ValidationError):
        ToolInput.model_validate(tool_data(method="POST"))
    assert ToolInput.model_validate(search_config()).method == "POST"
    assert WebSearch(query="  查询  ").query == "查询"


def test_catalog_defaults_and_unconfigured_exclusion(client):
    """默认目录可见、默认助手授权；未配置时不发送给模型，不扩展旧版本。"""
    _, _, task, _ = setup_task(client)
    rows = client.get("/api/v1/me/tools").json()["items"]
    row = next(item for item in rows if item["name"] == "web_search")
    assert row["builtin"] and row["enabled"] and not row["ready"]
    assert "web_search" in AgentConfig(name="test").tools
    with Session(client.app.state.database) as db:
        assert "web_search" not in db.get(Run, task["id"]).snapshot["tools"]
    assert "web_search" not in AgentConfig(name="旧助手", tools=["text_stats"]).tools


def test_production_execution_freezes_search_and_revokes_cached_results(client, monkeypatch):
    """真实执行链冻结搜索契约，账本只保存摘录，停用后不复用缓存。"""
    current = client.app.state.settings
    current.tavily_api_key = settings().tavily_api_key
    current.tool_http_allowed_hosts = ["api.tavily.com"]
    current.task_budget_enabled = False
    _, _, task, _ = setup_task(client, tools=["web_search"])
    records = mock_http(monkeypatch, payload())
    call = ToolCall(id="search", name="web_search", arguments={"query": "公开资料"})
    provider = ScriptedProvider([Reply(calls=[call]), Reply()])
    Execution(client.app.state.database, current, task["id"]).execute(lambda *args: provider)
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        assert run.status == "succeeded"
        assert run.snapshot["tool_catalog"]["web_search"]["schema"]["parameters"]["required"] == [
            "query"
        ]
        saved = db.scalar(select(ToolExecution).where(ToolExecution.run_id == run.id))
        assert saved.status == "succeeded" and saved.result["source"] == "Tavily"
        assert "fixture-tavily-secret" not in json.dumps([run.snapshot, saved.result])
        snapshot = run.snapshot
        owner = run.owner_id
    assert len(records) == 1
    identity = login(client)
    headers = {**ORIGIN, "X-CSRF-Token": identity["csrf_token"]}
    assert (
        client.patch(
            "/api/v1/me/tools/web_search", json={"enabled": False}, headers=headers
        ).status_code
        == 200
    )
    with Session(client.app.state.database) as db:
        with pytest.raises(ProviderFailure, match="tool_unavailable"):
            bound_tool(db, owner, owner, snapshot, "web_search", current)


def test_large_pages_above_old_cap_are_reduced_to_excerpts(monkeypatch):
    """复现原 256 KiB 阈值导致正常网页失败的问题，读取后仍只给模型短摘录。"""
    data = payload()
    data["results"][0]["raw_content"] = "正文" * 80000
    records = mock_http(monkeypatch, data)
    result = execute_http(pin(), {"query": "测试"}, settings(), lambda: None)
    assert result["ok"] is True
    assert len(records) == 1
    assert len(result["results"][0]["raw_content"].encode()) <= 1300
    assert len(json.dumps(result, ensure_ascii=False).encode()) < 16000


def test_oversized_raw_response_falls_back_once_with_shared_deadline(monkeypatch):
    """正文过大后同一工具调用降级摘要；结果注明缺失正文，截止时间不重置。"""
    from app.tools import http_runtime

    calls = []

    def request(pin_value, arguments, current, guard, deadline, *, raw_content):
        """记录两次请求选项与截止时间，不实际访问网络。"""
        calls.append((deadline, raw_content))
        return (
            {"ok": False, "error_code": "tool_result_too_large"}
            if raw_content
            else {
                "ok": True,
                "source": "Tavily",
                "results": [],
            }
        )

    monkeypatch.setattr(http_runtime, "_execute_http", request)
    result = execute_http(pin(), {"query": "测试"}, settings(), lambda: None)
    assert len(calls) == 2 and calls[0][0] == calls[1][0]
    assert [item[1] for item in calls] == [True, False]
    assert result["raw_content_omitted"] is True and result["content_mode"] == "snippets"


def test_snippet_request_omits_full_page_content(monkeypatch):
    """降级请求确实禁用完整正文，而非再次发送相同的过大请求。"""
    import time

    from app.tools import http_runtime

    records = mock_http(monkeypatch, payload())
    result = http_runtime._execute_http(
        pin(),
        {"query": "测试"},
        settings(),
        lambda: None,
        time.monotonic() + 20,
        raw_content=False,
    )
    assert result["ok"] is True
    assert json.loads(records[0]["body"])["include_raw_content"] is False

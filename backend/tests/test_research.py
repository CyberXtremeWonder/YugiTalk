"""研究闭环、引用、所有者权限和中断恢复回归；网络与模型使用明确替身。"""

import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_identity import ORIGIN, login

from app.core.entities import now
from app.providers.client import ModelEvent, ProviderFailure, Reply
from app.research.contracts import Report
from app.research.models import ResearchProject, ResearchReport, ResearchSource
from app.research.reader import read_source
from app.research.runner import ResearchRunner, render_report
from app.tasks.execution import Execution
from app.tasks.models import Run

pytest_plugins = ["test_identity"]


def setup_research(client, tools=None):
    from pydantic import SecretStr

    client.app.state.settings.tool_http_allowed_hosts = ["api.tavily.com"]
    client.app.state.settings.tavily_api_key = SecretStr("research-test-not-a-real-key")
    identity = login(client)
    headers = {**ORIGIN, "X-CSRF-Token": identity["csrf_token"]}
    agent = client.post(
        "/api/v1/me/agents",
        json={
            "name": "研究助手",
            "tools": tools or ["web_search"],
        },
        headers=headers,
    ).json()
    project = client.post(
        "/api/v1/research",
        json={
            "title": "研究测试",
            "question": "比较方案的限制",
            "max_rounds": 2,
        },
        headers=headers,
    ).json()
    conversation = client.post(
        "/api/v1/me/conversations",
        json={
            "title": "研究会话",
        },
        headers=headers,
    ).json()
    body = {
        "conversation_id": conversation["id"],
        "agent_version_id": agent["version_id"],
        "message": "比较方案的限制",
        "research_project_id": project["id"],
    }
    headers["Idempotency-Key"] = str(uuid4())
    client.app.state.dispatch_run = lambda key: None
    return project, headers, body


def test_mock_project_report_and_idempotency(client):
    project, headers, body = setup_research(client)
    response = client.post("/api/v1/runs", json=body, headers=headers)
    assert response.status_code == 202, response.text
    assert client.post("/api/v1/runs", json=body, headers=headers).json() == response.json()
    Execution(client.app.state.database, client.app.state.settings, response.json()["id"]).execute()
    result = client.get(f"/api/v1/research/{project['id']}").json()
    assert result["run"]["status"] == "succeeded", result["run"]
    assert "Mock" in result["reports"][0]["markdown"]
    assert result["sources"] == []
    headers["Idempotency-Key"] = str(uuid4())
    assert client.post("/api/v1/runs", json=body, headers=headers).status_code == 409
    # 报告独立于对话保留，不用 Run 外键阻塞既有删除流程。
    assert (
        client.delete(
            f"/api/v1/conversations/{body['conversation_id']}", headers=headers
        ).status_code
        == 204
    )
    assert client.get(f"/api/v1/research/{project['id']}").json()["reports"]


def test_project_owner_and_csrf(client):
    project, _, body = setup_research(client)
    assert (
        client.post(
            "/api/v1/research",
            json={
                "title": "x",
                "question": "q",
            },
        ).status_code
        == 403
    )
    identity = login(client, "other@example.test")
    assert client.get(f"/api/v1/research/{project['id']}").status_code == 404
    assert client.get("/api/v1/research").json()["items"] == []
    headers = {**ORIGIN, "X-CSRF-Token": identity["csrf_token"], "Idempotency-Key": "other"}
    assert client.post("/api/v1/runs", json=body, headers=headers).status_code == 404


def test_research_cannot_expand_tool_authorization(client):
    _, headers, body = setup_research(client, ["text_stats"])
    response = client.post("/api/v1/runs", json=body, headers=headers)
    assert response.status_code == 409


def test_empty_goal_or_no_sources_rejected(client):
    identity = login(client)
    headers = {**ORIGIN, "X-CSRF-Token": identity["csrf_token"]}
    for body in [
        {"title": " ", "question": "q"},
        {"title": "x", "question": "q", "web_enabled": False},
    ]:
        assert client.post("/api/v1/research", json=body, headers=headers).status_code == 422


def test_migration_roundtrip_preserves_existing_tables():
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect, text

    path = Path(__file__).parents[1] / "migrations/versions/0023_research.py"
    spec = importlib.util.spec_from_file_location("research_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id VARCHAR(36) PRIMARY KEY)"))
        connection.execute(text("INSERT INTO users VALUES ('existing')"))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        assert {"research_projects", "research_sources", "research_reports"} <= set(
            inspect(connection).get_table_names()
        )
        migration.downgrade()
        assert inspect(connection).get_table_names() == ["users"]
        assert connection.execute(text("SELECT id FROM users")).scalar() == "existing"
    engine.dispose()


@pytest.mark.parametrize("identity,quote", [("unknown", "原文"), ("R-1", "不存在的句子")])
def test_fabricated_reference_or_quote_rejected(identity, quote):
    report = Report(
        findings=[
            {
                "text": "结论",
                "kind": "fact",
                "evidence": [{"evidence_id": identity, "quote": quote}],
            }
        ],
        limitations=["待验证"],
    )
    with pytest.raises(ProviderFailure, match="invalid_citation"):
        render_report(
            "报告",
            report,
            [
                {
                    "evidence_id": "R-1",
                    "content": "原文",
                    "title": "来源",
                    "url": "https://example.org",
                }
            ],
        )


@pytest.mark.parametrize("url", ["http://example.org", "https://127.0.0.1", "file:///etc/passwd"])
def test_reader_rejects_non_public_or_non_https(url):
    assert read_source(url, lambda: None)["read_status"] == "https_required"


def test_deleted_internal_source_blocks_snapshot_and_report(client):
    from app.rag.models import Document, KnowledgeBase

    project, headers, body = setup_research(client)
    run = client.post("/api/v1/runs", json=body, headers=headers).json()
    Execution(client.app.state.database, client.app.state.settings, run["id"]).execute()
    with Session(client.app.state.database) as db:
        owner = db.get(ResearchProject, project["id"]).owner_id
        base = KnowledgeBase(account_id=owner, name="内部资料")
        db.add(base)
        db.flush()
        doc = Document(
            knowledge_base_id=base.id,
            account_id=owner,
            filename="制度.txt",
            content_hash="hash",
            object_key="test",
            size=1,
            status="ready",
            embedding_version="v1",
            collection_name="c1",
        )
        db.add(doc)
        db.flush()
        db.add(
            ResearchSource(
                project_id=project["id"],
                identity="internal",
                data={
                    "kind": "knowledge",
                    "document_id": doc.id,
                    "knowledge_base_id": base.id,
                    "index_version": doc.index_version,
                    "embedding_version": "v1",
                    "collection_name": "c1",
                    "content": "受限原文",
                },
            )
        )
        db.commit()
        assert client.get(f"/api/v1/research/{project['id']}").status_code == 200
        base.deleted = True
        db.commit()
    response = client.get(f"/api/v1/research/{project['id']}")
    assert response.status_code == 409
    assert "受限原文" not in response.text


def test_cancelled_research_stops_before_model_or_network(client):
    project, headers, body = setup_research(client)
    run = client.post("/api/v1/runs", json=body, headers=headers).json()
    assert client.post(f"/api/v1/runs/{run['id']}/cancel", headers=headers).status_code == 200
    Execution(client.app.state.database, client.app.state.settings, run["id"]).execute(
        lambda *args: pytest.fail("取消后不能调用模型")
    )
    result = client.get(f"/api/v1/research/{project['id']}").json()
    assert result["run"]["status"] == "cancelled"
    assert result["reports"] == []


def test_reader_extracts_body_without_running_embedded_instructions(monkeypatch):
    from email.message import Message

    class Response:
        status = 200
        headers = Message()
        headers["Content-Type"] = "text/html; charset=utf-8"
        payload = "<h1>原始内容</h1><script>steal()</script><p>事实正文</p>".encode()

        def read(self, size):
            data, self.payload = self.payload, b""
            return data

    class Connection:
        sock = None

        def __init__(self, *args):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr("app.research.reader.resolve_public", lambda *args: "93.184.216.34")
    monkeypatch.setattr("app.research.reader.PinnedHTTPSConnection", Connection)
    result = read_source("https://example.org/a", lambda: None)
    assert result["read_status"] == "read"
    assert result["content"] == "原始内容\n事实正文"
    assert len(result["content_hash"]) == 64


class Script:
    guard = staticmethod(lambda: None)

    def stream(self, messages, tools, max_tokens):
        assert not tools
        instruction = messages[0]["content"]
        if "将研究目标" in instruction:
            data = {
                "questions": [
                    {"title": "方案限制", "query": "方案限制", "criterion": "找到原始说明"}
                ]
            }
        elif "仅判断" in instruction:
            data = {"status": "partial", "gaps": ["缺少独立验证"], "next_query": "独立验证"}
        else:
            sources = json.loads(messages[-1]["content"])["sources"]
            data = {
                "findings": [
                    {
                        "text": "需要额外硬件",
                        "kind": "fact",
                        "evidence": [
                            {
                                "evidence_id": sources[0]["evidence_id"],
                                "quote": "需要额外硬件",
                            }
                        ],
                    }
                ],
                "limitations": ["缺少独立验证"],
            }
        self.guard()
        yield ModelEvent("text", text=json.dumps(data, ensure_ascii=False))
        yield ModelEvent("reply", reply=Reply(input_tokens=3, output_tokens=5))


def test_research_reads_deduplicates_and_generates_report(client, monkeypatch):
    project, headers, body = setup_research(client)
    run = client.post("/api/v1/runs", json=body, headers=headers).json()
    settings = client.app.state.settings.model_copy(update={"task_budget_enabled": False})
    with Session(client.app.state.database) as db:
        row = db.get(Run, run["id"])
        row.snapshot = {**row.snapshot, "provider": "openai_compatible", "base_url": ""}
        db.commit()
    queries, reads = [], []

    def search(self, call, step):
        queries.append(call.arguments["query"])
        return {
            "ok": True,
            "results": [{"title": "原始说明", "url": "https://example.org/a", "content": "摘要"}],
        }

    def read(url, guard):
        guard()
        reads.append(url)
        return {"read_status": "read", "content": "方案需要额外硬件。", "content_hash": "hash"}

    monkeypatch.setattr(Execution, "tool", search)
    monkeypatch.setattr("app.research.runner.read_source", read)
    Execution(client.app.state.database, settings, run["id"]).execute(lambda *args: Script())
    result = client.get(f"/api/v1/research/{project['id']}").json()
    assert result["run"]["status"] == "succeeded", result["run"]
    assert queries == ["方案限制", "独立验证"]
    assert len(reads) == 1
    assert len(result["sources"]) == 1
    assert "[^1]" in result["reports"][0]["markdown"]
    assert result["state"]["input_tokens"] == 12
    # 报告不复制进聊天，资料撤销后不存在绕过研究接口读取报告的副本。
    messages = client.get(f"/api/v1/conversations/{body['conversation_id']}/messages").json()
    assert "额外硬件" not in messages["items"][-1]["content"]


def test_resume_uses_saved_model_stage_and_report(client):
    project, headers, body = setup_research(client)
    run = client.post("/api/v1/runs", json=body, headers=headers).json()
    execution = Execution(client.app.state.database, client.app.state.settings, run["id"])
    with Session(execution.engine) as db:
        row = db.get(Run, run["id"])
        row.status = "running"
        row.lease_token = execution.token
        row.lease_until = now() + timedelta(seconds=100)
        row.slice_started_at = now()
        snapshot = dict(row.snapshot)
        project_row = db.get(ResearchProject, project["id"])
        project_row.state = {
            **project_row.state,
            "saved": {
                "questions": [
                    {
                        "title": "已保存",
                        "query": "q",
                        "criterion": "c",
                    }
                ]
            },
        }
        db.commit()
    from app.research.contracts import Plan

    runner = ResearchRunner(execution, SimpleNamespace(), snapshot)
    # 不允许触发任何模型调用。
    assert runner.model("saved", "", {}, Plan).questions[0].title == "已保存"
    with Session(execution.engine) as db:
        db.add(
            ResearchReport(
                project_id=project["id"],
                run_id=run["id"],
                version=1,
                markdown="已提交报告",
                data={},
            )
        )
        db.commit()
    assert runner.run()[0] == "已提交报告"
    with Session(execution.engine) as db:
        assert len(list(db.scalars(select(ResearchSource)))) == 0

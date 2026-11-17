"""研究复用已有证据、结构化报告输出控制及恢复回归，不访问真实服务。"""

import hashlib
import json
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.orm import Session
from test_research import setup_research

from app.core.entities import now
from app.providers.client import HTTPProvider, ModelEvent, Reply
from app.rag.models import Document, KnowledgeBase
from app.research.contracts import Report
from app.research.runner import ResearchRunner
from app.tasks.execution import Execution
from app.tasks.ledger_models import ToolExecution
from app.tasks.models import Run
from app.tasks.reliability import retryable_run

pytest_plugins = ["test_identity"]


def runner_for(client, provider):
    """建立持有有效租约的研究任务，供单阶段和缓存测试使用。"""
    project, headers, body = setup_research(client)
    task = client.post("/api/v1/runs", json=body, headers=headers).json()
    execution = Execution(client.app.state.database, client.app.state.settings, task["id"])
    execution.settings.task_budget_enabled = False
    with Session(execution.engine) as db:
        run = db.get(Run, task["id"])
        run.status, run.lease_token = "running", execution.token
        run.lease_until = now() + timedelta(seconds=300)
        run.slice_started_at = now()
        snapshot = {**run.snapshot, "provider": "openai_compatible", "max_output_tokens": 100000}
        db.commit()
    return ResearchRunner(execution, provider, snapshot), project["id"]


def seed_web(runner):
    """模拟外层已成功搜索的资料，正文仍明确属于搜索摘录。"""
    with Session(runner.execution.engine) as db:
        db.add(
            ToolExecution(
                run_id=runner.execution.key,
                call_key="1:outer-search",
                tool="web_search",
                arguments={"query": "方案限制", "max_results": 5},
                argument_hash="test",
                status="succeeded",
                result={
                    "ok": True,
                    "results": [
                        {
                            "url": "https://example.org/source",
                            "title": "原始说明",
                            "content": "方案需要额外硬件。",
                            "raw_content": "方案需要额外硬件。",
                        }
                    ],
                },
            )
        )
        db.commit()


@pytest.mark.parametrize("limit,expected", [(100000, 16000), (2048, 2048)])
def test_deepseek_report_disables_thinking_uses_json_and_respects_output_limit(
    client, limit, expected
):
    bodies = []

    def response(request):
        """检查真实协议请求参数，返回完整 JSON 流，不产生网络请求。"""
        body = json.loads(request.content)
        bodies.append(body)
        assert body["thinking"] == {"type": "disabled"}
        assert body["response_format"] == {"type": "json_object"}
        assert body["max_tokens"] == expected
        content = json.dumps({"findings": [], "limitations": ["协议测试"]}, ensure_ascii=False)
        frames = [
            {"choices": [{"delta": {"content": content}, "finish_reason": None}]},
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
            },
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
                + "data: [DONE]\n\n"
            ),
        )

    settings = client.app.state.settings.model_copy(
        update={
            "model_base_url": "https://provider.example/v1",
            "model_api_key": SecretStr("test"),
            "model_max_retries": 0,
        }
    )
    provider = HTTPProvider(
        settings, "openai_compatible", "deepseek-flash", httpx.MockTransport(response)
    )
    runner, _ = runner_for(client, provider)
    runner.snapshot["max_output_tokens"] = limit
    report = runner.model("report_draft", "生成报告 JSON", {}, Report)
    assert report.limitations == ["协议测试"]
    assert provider.request_purpose == "report_draft" and len(bodies) == 1


def test_sufficient_outer_evidence_skips_all_new_searches(client, monkeypatch):
    calls = []

    class Provider:
        guard = staticmethod(lambda: None)

        def stream(self, messages, tools, max_tokens):
            """先确认已有证据充分，然后直接撰写报告。"""
            instruction = messages[0]["content"]
            context = json.loads(messages[-1]["content"])
            assert context["sources"]
            if "将研究目标" in instruction:
                calls.append("plan")
                data = {
                    "questions": [{"title": "限制", "query": "方案限制", "criterion": "核对原文"}]
                }
            elif "仅判断" in instruction:
                calls.append("assess")
                data = {"status": "sufficient", "gaps": [], "next_query": ""}
            else:
                calls.append("report")
                data = {
                    "findings": [
                        {
                            "text": "需要额外硬件",
                            "kind": "fact",
                            "evidence": [
                                {
                                    "evidence_id": context["sources"][0]["evidence_id"],
                                    "quote": "需要额外硬件",
                                }
                            ],
                        }
                    ],
                    "limitations": ["基于搜索摘录，未读取网页全文。"],
                }
            yield ModelEvent("text", text=json.dumps(data, ensure_ascii=False))
            yield ModelEvent("reply", reply=Reply())

    runner, _ = runner_for(client, Provider())
    seed_web(runner)
    monkeypatch.setattr(Execution, "tool", lambda *args: pytest.fail("不得重新搜索"))
    monkeypatch.setattr(
        "app.research.runner.read_source", lambda *args: pytest.fail("现有证据已充分")
    )
    markdown, _, _ = runner.run()
    assert "需要额外硬件" in markdown and calls == ["plan", "assess", "report"]
    assert runner.sources()[0]["read_status"] == "search_excerpt"


def test_identical_web_query_reuses_outer_result_and_only_expands_original_page(
    client, monkeypatch
):
    runner, _ = runner_for(client, type("Provider", (), {})())
    seed_web(runner)
    options = runner.load()[1]
    runner.inherit_sources(options)
    evidence = runner.sources()[0]["evidence_id"]
    monkeypatch.setattr(Execution, "tool", lambda *args: pytest.fail("同一查询不得重新搜索"))
    reads = []

    def read(url, guard):
        """补读全文有别于再次搜索，保留原来的证据身份。"""
        reads.append(url)
        return {"read_status": "read", "content": "方案需要额外硬件。完整原文。"}

    monkeypatch.setattr("app.research.runner.read_source", read)
    runner.gather("gap", "方案限制", options)
    assert reads == ["https://example.org/source"]
    assert runner.sources()[0]["evidence_id"] == evidence
    assert runner.sources()[0]["read_status"] == "read"


def test_knowledge_cache_rechecks_scope_version(client):
    runner, _ = runner_for(client, type("Provider", (), {})())
    with Session(runner.execution.engine) as db:
        run = db.get(Run, runner.execution.key)
        base = KnowledgeBase(account_id=run.owner_id, name="资料")
        db.add(base)
        db.flush()
        doc = Document(
            knowledge_base_id=base.id,
            account_id=run.owner_id,
            filename="资料.txt",
            content_hash="hash",
            object_key="test",
            size=1,
            status="ready",
            index_version=1,
            embedding_version="v1",
            collection_name="c1",
        )
        db.add(doc)
        db.flush()
        base_id, doc_id = base.id, doc.id
        versions = [(doc.id, 1, "c1", "v1")]
        run.checkpoint = {
            "subagents": {
                "direct": {
                    "tools": {
                        "one": {
                            "search_key": hashlib.sha256(
                                json.dumps([base_id, "查询"]).encode()
                            ).hexdigest(),
                            "search_scope": hashlib.sha256(
                                json.dumps(versions).encode()
                            ).hexdigest(),
                            "result": {"ok": True, "sources": []},
                        }
                    }
                }
            }
        }
        db.commit()
    assert runner.cached_knowledge(base_id, "查询") == []
    with Session(runner.execution.engine) as db:
        db.get(Document, doc_id).index_version = 2
        db.commit()
    assert runner.cached_knowledge(base_id, "查询") is None


def test_report_recovery_reuses_completed_stages(client, monkeypatch):
    class Provider:
        guard = staticmethod(lambda: None)

        def stream(self, messages, tools, max_tokens):
            assert "形成研究发现" in messages[0]["content"]
            yield ModelEvent("text", text='{"findings": [], "limitations": ["恢复测试"]}')
            yield ModelEvent("reply", reply=Reply())

    runner, _ = runner_for(client, Provider())
    runner.save(
        plan={"questions": [{"title": "限制", "query": "方案限制", "criterion": "核对原文"}]},
        question_0_round_0=True,
        question_0_round_0_assessment={"status": "sufficient", "gaps": [], "next_query": ""},
    )
    monkeypatch.setattr(Execution, "tool", lambda *args: pytest.fail("恢复不得重复搜索"))
    markdown, _, _ = runner.run()
    assert "恢复测试" in markdown
    assert "report_draft" in runner.load()[0]


@pytest.mark.parametrize("research,expected", [(True, True), (False, False)])
def test_only_research_output_limit_can_resume(client, research, expected):
    runner, _ = runner_for(client, type("Provider", (), {})())
    with Session(runner.execution.engine) as db:
        run = db.get(Run, runner.execution.key)
        run.status, run.error_code = "failed", "model_output_limit"
        run.snapshot = {"research_project_id": "project"} if research else {}
        assert retryable_run(run) is expected

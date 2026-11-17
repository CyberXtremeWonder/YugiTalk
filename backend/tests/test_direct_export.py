"""只生成 PDF 时直接导出原答复，禁止额外检索或研究。"""

import io
import json
from uuid import uuid4

import pytest
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_agents import setup_task
from test_subagents import Script, tool

from app.research.conversation import export_only_request
from app.research.models import ResearchProject, ResearchReport
from app.tasks.execution import Execution, finish_run

pytest_plugins = ["test_identity"]


@pytest.mark.parametrize(
    "prompt",
    ["我要pdf文件", "转成PDF", "根据上述研究结果生成 Word", "把刚才内容导出 Markdown，不要联网"],
)
def test_existing_content_export_is_not_research(prompt):
    assert export_only_request(prompt)


@pytest.mark.parametrize(
    "prompt",
    ["搜索最新资料并生成 PDF", "研究这两种方案，生成 Word", "PDF 是什么", "比较数据库的区别"],
)
def test_research_and_general_questions_keep_normal_tools(prompt):
    assert not export_only_request(prompt)


def test_export_prior_answer_preserves_body_without_retrieval_or_research(client, monkeypatch):
    headers, body, first, _ = setup_task(client, "整理部署条件", tools=["text_stats"])
    original = (
        "# 部署条件\n\n需要 16 GB 内存，报销上限每晚 380 元。\n\n这是已有答复，不需要再次检索。"
    )
    finish_run(client.app.state.database, first["id"], "succeeded", None, original, 0, None, None)
    headers["Idempotency-Key"] = str(uuid4())
    failed = client.post(
        "/api/v1/runs", json={**body, "message": "先前的导出请求"}, headers=headers
    ).json()
    finish_run(
        client.app.state.database,
        failed["id"],
        "failed",
        "model_output_limit",
        "尚未完成的答复片段",
        0,
        None,
        None,
    )
    headers["Idempotency-Key"] = str(uuid4())
    task = client.post(
        "/api/v1/runs", json={**body, "message": "我要pdf文件"}, headers=headers
    ).json()

    def forbidden(*args, **kwargs):
        """文件导出不得执行任何搜索或研究链路。"""
        pytest.fail("单纯生成 PDF 不应检索或研究")

    monkeypatch.setattr(Execution, "tool", forbidden)
    monkeypatch.setattr("app.research.runner.ResearchRunner.run", forbidden)
    monkeypatch.setattr("app.rag.indexing.retrieve", forbidden)
    monkeypatch.setattr("app.research.runner.read_source", forbidden)

    def complete(messages):
        """下载元数据真实返回后，助手才可确认文件完成。"""
        result = json.loads(messages[-1]["content"])
        assert result["ok"] and result["files"][0]["filename"] == "部署条件.pdf"
        return "PDF 已生成，请下载附件。"

    provider = Script(
        [
            tool("export_document", title="部署条件", formats=["pdf"], source="latest_answer"),
            complete,
        ],
        ["export_document"],
    )
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(
        lambda *args: provider
    )
    status = client.get(f"/api/v1/runs/{task['id']}").json()
    assert status["status"] == "succeeded", status
    messages = client.get(f"/api/v1/conversations/{body['conversation_id']}/messages").json()[
        "items"
    ]
    file = messages[-1]["attachments"][0]
    downloaded = client.get(file["download_url"])
    text = "\n".join(
        page.extract_text() for page in PdfReader(io.BytesIO(downloaded.content)).pages
    )
    assert "16 GB" in text and "380" in text
    assert "研究发现" not in text
    with Session(client.app.state.database) as db:
        assert db.scalar(select(ResearchReport)).markdown == original
        assert db.scalar(select(ResearchProject)).options["export_only"] is True


def test_pdf_request_with_no_previous_answer_asks_for_content(client):
    _, body, task, _ = setup_task(client, "给我PDF", tools=["text_stats"])

    def missing(messages):
        """没有正文时应澄清，不能用检索结果填补。"""
        result = json.loads(messages[-1]["content"])
        assert result["error_code"] == "export_content_required"
        return "请提供需要生成 PDF 的正文。"

    provider = Script(
        [tool("export_document", title="文档", formats=["pdf"]), missing], ["export_document"]
    )
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(
        lambda *args: provider
    )
    assert client.get(f"/api/v1/runs/{task['id']}").json()["status"] == "succeeded"
    messages = client.get(f"/api/v1/conversations/{body['conversation_id']}/messages").json()[
        "items"
    ]
    assert messages[-1]["attachments"] == []
    with Session(client.app.state.database) as db:
        assert db.scalar(select(ResearchProject)) is None


def test_export_request_cannot_call_knowledge_search_even_if_model_proposes_it(client, monkeypatch):
    _, _, task, _ = setup_task(client, "我要pdf文件", tools=["text_stats"])
    monkeypatch.setattr(Execution, "tool", lambda *args: pytest.fail("不能执行额外工具"))
    provider = Script(
        [tool("knowledge_search", knowledge_base_id=str(uuid4()), query="额外资料")],
        ["export_document"],
    )
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(
        lambda *args: provider
    )
    result = client.get(f"/api/v1/runs/{task['id']}").json()
    assert result["error_code"] == "tool_not_authorized"

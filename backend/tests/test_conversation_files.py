"""对话自然语言交付文件、历史下载及撤权行为回归，不调用付费服务。"""

import io

from docx import Document
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_agents import setup_task
from test_identity import login

from app.research.models import ResearchProject, ResearchReport, ResearchSource
from app.tasks.execution import Execution

pytest_plugins = ["test_identity"]


def generate(client):
    """正常聊天提交即可触发文件工具，客户端不传模式或研究项目。"""
    headers, body, run, _ = setup_task(
        client,
        "请根据以下资料生成 Word、PDF 和 Markdown 文件：项目使用中文检索。",
        tools=["text_stats"],
    )
    Execution(client.app.state.database, client.app.state.settings, run["id"]).execute()
    result = client.get(f"/api/v1/runs/{run['id']}").json()
    assert result["status"] == "succeeded", result
    messages = client.get(f"/api/v1/conversations/{body['conversation_id']}/messages").json()
    files = messages["items"][-1]["attachments"]
    assert {f["filename"].rsplit(".", 1)[1] for f in files} == {"docx", "pdf", "md"}
    return headers, body, files


def test_chat_request_delivers_three_valid_persistent_files(client):
    _, body, files = generate(client)
    downloaded = {}
    for file in files:
        response = client.get(file["download_url"])
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "attachment;" in response.headers["content-disposition"]
        assert len(response.content) == file["size"]
        format = file["filename"].rsplit(".", 1)[1]
        downloaded[format] = response.content
    document = Document(io.BytesIO(downloaded["docx"]))
    assert "研究报告" in document.paragraphs[0].text
    assert "Mock" in "\n".join(p.text for p in document.paragraphs)
    pdf = PdfReader(io.BytesIO(downloaded["pdf"]))
    assert len(pdf.pages) >= 1
    assert "研究报告" in pdf.pages[0].extract_text()
    assert "Mock" in downloaded["md"].decode("utf-8")
    # 重读聊天历史仍返回同一批附件，不需要回到其他功能页面。
    again = client.get(f"/api/v1/conversations/{body['conversation_id']}/messages").json()
    assert again["items"][-1]["attachments"] == files
    with Session(client.app.state.database) as db:
        assert len(list(db.scalars(select(ResearchProject)))) == 1
        assert len(list(db.scalars(select(ResearchReport)))) == 1


def test_file_download_is_private_and_invalid_format_rejected(client):
    _, _, files = generate(client)
    path = files[0]["download_url"]
    assert client.get(path.rsplit("/", 1)[0] + "/exe").status_code == 422
    login(client, "other@example.test")
    assert client.get(path).status_code == 404


def test_revoked_source_disables_chat_attachment_and_download(client):
    _, body, files = generate(client)
    with Session(client.app.state.database) as db:
        project = db.scalar(select(ResearchProject))
        db.add(
            ResearchSource(
                project_id=project.id,
                identity="removed",
                data={
                    "kind": "knowledge",
                    "document_id": "removed",
                    "knowledge_base_id": "removed",
                },
            )
        )
        db.commit()
    assert client.get(files[0]["download_url"]).status_code == 409
    history = client.get(f"/api/v1/conversations/{body['conversation_id']}/messages").json()
    assert history["items"][-1]["attachments"]
    assert all(
        not f["available"] and f["download_url"] is None
        for f in history["items"][-1]["attachments"]
    )


def test_normal_conversation_does_not_create_research_or_files(client):
    _, body, run, _ = setup_task(client, "你好", tools=["text_stats"])
    Execution(client.app.state.database, client.app.state.settings, run["id"]).execute()
    messages = client.get(f"/api/v1/conversations/{body['conversation_id']}/messages").json()
    assert messages["items"][-1]["attachments"] == []
    with Session(client.app.state.database) as db:
        assert db.scalar(select(ResearchProject)) is None


def test_model_cannot_invent_user_material(client):
    from test_subagents import WORKER_TOOLS, Script, tool

    _, body, run, _ = setup_task(client, "请研究这些资料并生成 Word 文件", tools=["text_stats"])

    def rejected(messages):
        """伪造资料返回可修正的工具错误，不创建研究或附件。"""
        import json

        result = json.loads(messages[-1]["content"])
        assert result["error_code"] == "research_material_unverified"
        return "请提供需要整理的原始资料。"

    provider = Script(
        [
            tool(
                "research_report",
                title="报告",
                question="整理材料",
                formats=["docx"],
                web_enabled=False,
                user_material="用户没有提供的研究结论",
            ),
            rejected,
        ],
        WORKER_TOOLS,
    )
    Execution(client.app.state.database, client.app.state.settings, run["id"]).execute(
        lambda *args: provider
    )
    result = client.get(f"/api/v1/runs/{run['id']}").json()
    assert result["status"] == "succeeded", result
    messages = client.get(f"/api/v1/conversations/{body['conversation_id']}/messages").json()
    assert messages["items"][-1]["attachments"] == []
    with Session(client.app.state.database) as db:
        assert db.scalar(select(ResearchProject)) is None

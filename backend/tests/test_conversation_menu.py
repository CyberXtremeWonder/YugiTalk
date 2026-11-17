"""最近对话的时间分页与完整文本导出，所有请求复用本人会话权限。"""

from datetime import timedelta
from io import BytesIO
from uuid import uuid4

import pytest
from docx import Document
from pypdf import PdfReader
from sqlalchemy.orm import Session
from test_agents import setup_task
from test_identity import login

from app.chat.models import Conversation, Message
from app.core.entities import now

pytest_plugins = ["test_identity"]


def test_recent_conversations_order_by_activity_with_stable_pagination(client):
    _, body, _, _ = setup_task(client)
    start = now() - timedelta(days=3)
    with Session(client.app.state.database) as db:
        original = db.get(Conversation, body["conversation_id"])
        original.created_at = start
        db.query(Message).filter(Message.conversation_id == original.id).delete()
        db.add(Message(conversation_id=original.id, sequence=1, role="user", content="最近补充",
                       created_at=start + timedelta(days=3)))
        rows = [original]
        for title in ("同一时间的新对话 A", "同一时间的新对话 B"):
            row = Conversation(id=str(uuid4()), account_id=original.account_id,
                               owner_id=original.owner_id, title=title,
                               created_at=start + timedelta(days=2))
            db.add(row)
            rows.append(row)
        expected = [original.id, *sorted([row.id for row in rows[1:]], reverse=True)]
        db.commit()
    received, cursor = [], None
    for _ in range(3):
        response = client.get("/api/v1/me/conversations", params={
            "limit": 1, **({"cursor": cursor} if cursor else {}),
        })
        assert response.status_code == 200
        page = response.json()
        received.extend(row["id"] for row in page["items"])
        assert page["items"][0]["updated_at"]
        cursor = page["next_cursor"]
    assert received == expected and cursor is None
    login(client, "other@example.test")
    assert client.get(f"/api/v1/me/conversations?cursor={expected[0]}").status_code == 404


@pytest.mark.parametrize("format", ["pdf", "docx", "txt", "json"])
def test_exports_include_entire_conversation_and_remain_private(client, format):
    _, body, _, _ = setup_task(client, "第一条中文问题")
    key = body["conversation_id"]
    with Session(client.app.state.database) as db:
        row = db.get(Conversation, key)
        row.title = '测试对话/下载"标题'
        for seq in range(2, 25):
            db.add(Message(conversation_id=key, sequence=seq,
                           role="assistant" if seq % 2 == 0 else "user",
                           content=f"第{seq}条完整消息 <tag> & 中文内容"))
        db.commit()
    response = client.get(f"/api/v1/conversations/{key}/export?format={format}")
    assert response.status_code == 200, response.text[:100]
    assert "attachment;" in response.headers["content-disposition"]
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"
    if format == "json":
        data = response.json()
        assert [m["sequence"] for m in data["messages"]] == list(range(1, 25))
        assert data["messages"][-1]["content"] == "第24条完整消息 <tag> & 中文内容"
        assert data["messages"][-1]["created_at"]
    elif format == "txt":
        assert "第一条中文问题" in response.text and "第24条完整消息" in response.text
        assert response.headers["content-type"].startswith("text/plain")
    elif format == "docx":
        text = "\n".join(p.text for p in Document(BytesIO(response.content)).paragraphs)
        assert "第一条中文问题" in text and "第24条完整消息" in text
    else:
        assert response.content.startswith(b"%PDF-")
        pages = PdfReader(BytesIO(response.content)).pages
        text = "\n".join(page.extract_text() for page in pages)
        assert "第一条中文问题" in text and "第24条完整消息" in text
    login(client, "other@example.test")
    assert client.get(f"/api/v1/conversations/{key}/export?format={format}").status_code == 404


def test_markdown_export_is_not_offered(client):
    _, body, _, _ = setup_task(client)
    response = client.get(f"/api/v1/conversations/{body['conversation_id']}/export?format=md")
    assert response.status_code == 422

"""对话上传格式、访问校验及文件内容进入模型上下文的回归。"""

import io
import json
from urllib.parse import quote

import pytest
from docx import Document
from reportlab.pdfgen.canvas import Canvas
from sqlalchemy.orm import Session
from test_agents import setup_task
from test_identity import ORIGIN, login

from app.chat.uploads import extract
from app.memory.context import build_messages
from app.providers.client import MockProvider
from app.tasks.execution import Execution
from app.tasks.models import Run

pytest_plugins = ["test_identity"]


@pytest.mark.parametrize("suffix", ["txt", "docx", "pdf"])
def test_upload_formats_and_auth(client, suffix):
    """真实生成 Word 和 PDF，要求会话、防伪令牌并返回可供消息使用的正文。"""
    stream = io.BytesIO()
    if suffix == "docx":
        document = Document()
        document.add_paragraph("attachment fixture")
        document.save(stream)
    elif suffix == "pdf":
        canvas = Canvas(stream)
        canvas.drawString(30, 700, "attachment fixture")
        canvas.save()
    else:
        stream.write(b"attachment fixture")
    headers = {**ORIGIN, "X-Filename": quote("资料." + suffix)}
    assert client.post(
        "/api/v1/chat/uploads", content=stream.getvalue(), headers=headers,
    ).status_code == 401
    identity = login(client)
    assert client.post(
        "/api/v1/chat/uploads", content=stream.getvalue(), headers=headers,
    ).status_code == 403
    response = client.post("/api/v1/chat/uploads", content=stream.getvalue(), headers={
        **headers, "X-CSRF-Token": identity["csrf_token"],
    })
    assert response.status_code == 200, response.text
    assert "attachment fixture" in response.json()["text"]


def test_large_text_is_explicitly_truncated_and_invalid_files_rejected(client):
    """大正文有可见截取标记，损坏文档、未知格式和扫描空文档不能假装成功。"""
    text, truncated = extract(b"x" * 100000, "large.txt")
    assert len(text) == 60000 and truncated
    identity = login(client)
    for filename, content in [("bad.docx", b"broken"), ("bad.exe", b"binary"), ("empty.txt", b"")]:
        response = client.post("/api/v1/chat/uploads", content=content, headers={
            **ORIGIN, "X-Filename": filename, "X-CSRF-Token": identity["csrf_token"],
        })
        assert response.status_code == 422


def test_uploaded_text_is_frozen_private_data_in_model_context_and_history(client):
    """提交后的附件进入用户级上下文，历史保留文件名，幂等键不能替换附件正文。"""
    headers, body, _, _ = setup_task(client, "请分析附件", tools=["text_stats"])
    body["uploaded_files"] = [{"filename": "note.txt", "size": 10,
                              "text": "private attachment fixture", "truncated": False}]
    headers = {**headers, "Idempotency-Key": "attachment-message"}
    response = client.post("/api/v1/runs", json=body, headers=headers)
    assert response.status_code == 202, response.text
    with Session(client.app.state.database) as db:
        run = db.get(Run, response.json()["id"])
        messages = build_messages(db, run)
        attached = [m for m in messages if "uploaded_files" in str(m.get("content"))]
        assert attached and all(m["role"] == "user" for m in attached)
        assert "private attachment fixture" in json.dumps(messages)
    history = client.get(f"/api/v1/conversations/{body['conversation_id']}/messages").json()
    assert history["items"][-1]["uploaded_files"][0]["filename"] == "note.txt"
    body["uploaded_files"][0]["text"] = "different"
    assert client.post("/api/v1/runs", json=body, headers=headers).status_code == 409
    seen = []

    class Capture(MockProvider):
        """只替代付费模型，执行层和附件上下文使用实际实现。"""

        def stream(self, messages, tools, max_tokens):
            """记录模型实际收到的附件文本。"""
            seen.extend(messages)
            yield from super().stream(messages, tools, max_tokens)

    Execution(client.app.state.database, client.app.state.settings, response.json()["id"]).execute(
        lambda *_: Capture(),
    )
    assert client.get(f"/api/v1/runs/{response.json()['id']}").json()["status"] == "succeeded"
    assert "private attachment fixture" in json.dumps(seen)

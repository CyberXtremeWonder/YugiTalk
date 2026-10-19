"""验证显式小节分块隔离主题，同时保持既有固定切块行为。"""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_knowledge import base

from app.rag.indexing import index_document
from app.rag.models import Chunk, Document, ParentChunk
from app.rag.parser import parse

pytest_plugins = ["test_identity", "test_knowledge"]


def test_sections_never_cross_headings_and_short_section_is_single_leaf():
    """长度不足七百的小节只形成一个叶子，相邻主题不混入。"""
    text = "## 支付服务\n" + "甲" * 640 + "\n\n## 订单服务\n" + "乙" * 20
    parts = parse(text.encode(), ".md", "markdown_sections")
    assert len(parts) == 2
    assert parts[0]["content"].endswith("甲" * 640)
    assert "订单服务" not in parts[0]["content"]
    assert "甲" not in parts[1]["content"]
    assert parts[0]["section"] != parts[1]["section"]


def test_fixed_strategy_keeps_existing_sliding_windows():
    """未选择新策略时，标题不改变原来七百字符和六百步长的结果。"""
    text = "## 标题\n" + "内容" * 1000
    parts = parse(text.encode(), ".md")
    assert [part["content"] for part in parts] == [
        text[start : start + 700].strip() for start in range(0, len(text), 600)
    ]
    assert all("section" not in part for part in parts)


@pytest.mark.parametrize("suffix,strategy", [(".txt", "markdown_sections"), (".md", "unknown")])
def test_invalid_strategy_is_explicit(suffix, strategy):
    """新策略仅用于显式指定的 Markdown，未知策略不能静默回退。"""
    with pytest.raises(ValueError, match="document_chunk_strategy_invalid"):
        parse(b"content", suffix, strategy)


def test_upload_index_parents_retry_and_duplicate_strategy(client, storage):
    """真实导入子进程传递策略，父块不跨小节，重试仍保留策略。"""
    _, headers, base_id = base(client)
    text = "## 第一节\n" + "甲" * 900 + "\n\n## 第二节\n" + "乙" * 50
    path = f"/api/v1/knowledge-bases/{base_id}/documents?filename=sections.md"
    response = client.post(
        path + "&chunk_strategy=markdown_sections", content=text.encode(), headers=headers
    )
    assert response.status_code == 202
    key = response.json()["id"]
    assert response.json()["chunk_strategy"] == "markdown-sections-700-parent-v1"
    engine, settings = client.app.state.database, client.app.state.settings
    index_document(engine, settings, key, 1)
    with Session(engine) as db:
        assert db.get(Document, key).status == "ready"
        parents = list(db.scalars(select(ParentChunk).where(ParentChunk.document_id == key)))
        assert len(parents) == 2
        assert sorted(parent.child_count for parent in parents) == [1, 2]
        assert all(not ("甲" in parent.content and "乙" in parent.content) for parent in parents)
        assert len(list(db.scalars(select(Chunk).where(Chunk.document_id == key)))) == 3
    assert client.post(path, content=text.encode(), headers=headers).status_code == 409
    assert client.post(f"/api/v1/documents/{key}/retry", headers=headers).status_code == 200
    index_document(engine, settings, key, 2)
    with Session(engine) as db:
        doc = db.get(Document, key)
        assert doc.status == "ready" and doc.chunk_strategy == "markdown-sections-700-parent-v1"

"""JSONL 记录边界、格式校验与上传索引链路。"""

import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_knowledge import base

from app.rag.indexing import index_document, parse_file, retrieve
from app.rag.models import Chunk, Document, ParentChunk
from app.rag.parser import parse

pytest_plugins = ["test_identity", "test_knowledge"]


@pytest.mark.parametrize("strategy", ["fixed", "auto", "long"])
def test_jsonl_records_keep_fields_unicode_and_boundaries(strategy):
    """BOM、空行、嵌套字段及合法 Unicode 均保留，各记录不混入同一父块。"""
    records = [
        {"question": "如何使用？", "answer": {"text": "答案\u2028续行", "enabled": False}},
        {"name": "另一条记录", "count": 0, "items": ["甲", "乙"], "empty": None},
    ]
    text = "\ufeff\n" + "\r\n\n".join(json.dumps(r, ensure_ascii=False) for r in records)
    parts = parse(text.encode(), ".jsonl", strategy)
    assert len(parts) == 2
    assert all(part["page"] is None for part in parts)
    assert [json.loads(part["content"]) for part in parts] == records
    field = "section" if strategy == "fixed" else "parent_group"
    assert parts[0][field] != parts[1][field]


@pytest.mark.parametrize("content", [b'{"ok": 1}\n{bad}', b'{\n"key": 1\n}', b'NaN'])
def test_jsonl_invalid_record_is_not_partially_indexed(content):
    """任一记录无效时，受限子进程返回明确错误而不是部分内容。"""
    with pytest.raises(ValueError, match="invalid_jsonl"):
        parse_file(content, ".jsonl", "auto")


def test_jsonl_blank_document_is_empty():
    """只有 BOM 和空白行时沿用空文档提示。"""
    with pytest.raises(ValueError, match="empty_document"):
        parse("\ufeff\n \n".encode(), ".jsonl", "auto")


def test_jsonl_title_text_is_readable_and_keeps_extra_fields():
    """正文还原换行和引号，标题及非正文元数据仍保留，旧检查点可沿用原格式。"""
    record = {"title": "标题", "text": '第一行。\n第二行说“你好”。', "url": "https://example.test"}
    raw = json.dumps(record).encode()
    content = parse(raw, ".jsonl", "fixed")[0]["content"]
    assert content == '标题\n\n第一行。\n第二行说“你好”。\n\n{"url": "https://example.test"}'
    assert json.loads(parse(raw, ".jsonl", "fixed", optimize_jsonl=False)[0]["content"]) == record


@pytest.mark.parametrize("strategy", ["auto", "fixed"])
def test_jsonl_upload_index_retrieve_download_and_retry(client, storage, strategy):
    """原文件可下载，全部记录完成索引并可检索，重试保留解析方式。"""
    identity, headers, base_id = base(client)
    records = [{"text": "支付服务使用DB1。"}, {"text": "负责人张三负责付款。"}]
    content = "\n".join(json.dumps(r) for r in records).encode()
    response = client.post(
        f"/api/v1/knowledge-bases/{base_id}/documents"
        f"?filename=records.JSONL&chunk_strategy={strategy}",
        content=content, headers=headers,
    )
    assert response.status_code == 202
    key = response.json()["id"]
    engine, settings = client.app.state.database, client.app.state.settings
    index_document(engine, settings, key, 1)
    with Session(engine) as db:
        assert db.get(Document, key).status == "ready"
        children = list(db.scalars(
            select(Chunk).where(Chunk.document_id == key).order_by(Chunk.ordinal),
        ))
        assert [child.content for child in children] == [record["text"] for record in records]
        parents = list(db.scalars(select(ParentChunk).where(ParentChunk.document_id == key)))
        assert len(parents) == 2
        owner = identity["user"]["id"]
        assert retrieve(db, settings, owner, owner, base_id, "支付服务使用DB1")
    assert client.get(f"/api/v1/documents/{key}/download").content == content
    assert client.post(f"/api/v1/documents/{key}/retry", headers=headers).status_code == 200
    index_document(engine, settings, key, 2)
    with Session(engine) as db:
        assert db.get(Document, key).status == "ready"
        assert db.get(Document, key).index_version == 2


def test_invalid_jsonl_reason_is_visible_after_upload(client, storage):
    """坏行错误穿过后台索引状态，可被前端解释。"""
    identity, headers, base_id = base(client)
    response = client.post(
        f"/api/v1/knowledge-bases/{base_id}/documents?filename=bad.jsonl",
        content=b'{"ok": 1}\n{bad}', headers=headers,
    )
    assert response.status_code == 202
    key = response.json()["id"]
    index_document(client.app.state.database, client.app.state.settings, key, 1)
    with Session(client.app.state.database) as db:
        document = db.get(Document, key)
        assert document.status == "failed"
        assert document.error_code == "invalid_jsonl"
        owner = identity["user"]["id"]
        assert retrieve(db, client.app.state.settings, owner, owner, base_id, "ok") == []

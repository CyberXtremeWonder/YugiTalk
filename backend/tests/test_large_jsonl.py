"""大 JSONL 的上传边界、有界读取和跨任务恢复。"""

import json
from tempfile import SpooledTemporaryFile

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_knowledge import base

from app.rag.indexing import index_document
from app.rag.jsonl_indexing import records
from app.rag.models import Chunk, Document, ParentChunk

pytest_plugins = ["test_identity", "test_knowledge"]


def test_prefetch_overlaps_consumer_and_propagates_errors():
    """下一批能与消费者并行，生产异常继续上报而非静默跳过记录。"""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from app.rag.jsonl_indexing import prefetch
    entered, release, stopped = Event(), Event(), Event()

    def source():
        yield 1
        entered.set()
        assert release.wait(2)
        yield 2
        raise ValueError("parse_failed")

    with ThreadPoolExecutor(max_workers=1) as pool:
        iterator = prefetch(source(), pool, stopped)
        assert next(iterator) == 1
        try:
            assert entered.wait(2)
        finally:
            release.set()
        assert next(iterator) == 2
        with pytest.raises(ValueError, match="parse_failed"):
            next(iterator)


def test_import_timings_excludes_nested_reads_and_accumulates(monkeypatch):
    """读取嵌在解析内时不重复累计，跨任务加载的计时继续增加。"""
    from app.rag.jsonl_indexing import ImportTimings
    clock = iter([0.0, 1.0, 3.0, 5.0])
    monkeypatch.setattr("app.rag.jsonl_indexing.time.perf_counter", lambda: next(clock))
    timer = ImportTimings({"reading": 4.0})
    with timer.measure("parsing"):
        with timer.measure("reading"):
            pass
    assert timer.snapshot() == {"reading": 6.0, "parsing": 3.0}


def test_records_cross_network_blocks_and_resume_offsets():
    """UTF-8 字符可跨网络块，检查点按原始字节而非字符计数。"""
    content = '\ufeff{"中文":1}\r\n\n{"name":"last"}'.encode()
    blocks = iter(content[i:i + 2] for i in range(0, len(content), 2))
    lines = list(records(blocks))
    assert b"".join(raw for _, raw in lines) == content
    assert lines[-1][0] == len(content)
    offset = lines[0][0]
    assert list(records(iter([content[offset:]]), offset)) == lines[1:]


def test_oversized_record_is_rejected_before_reading_whole_object(monkeypatch):
    """单行超限时立即停止后续 S3 读取。"""
    monkeypatch.setattr("app.rag.jsonl_indexing.MAX_FILE_BYTES", 4)

    def blocks():
        """第三块不应被拉取。"""
        yield b"123"
        yield b"456"
        pytest.fail("read beyond oversized record")

    with pytest.raises(ValueError, match="jsonl_record_too_large"):
        list(records(blocks()))


@pytest.mark.parametrize("parser_version", [1, 2])
def test_jsonl_continues_from_checkpoint_without_duplicates(
    client, storage, monkeypatch, parser_version,
):
    """各条记录分属独立任务，累计文本和子块可超过普通文档的总量上限。"""
    _, headers, base_id = base(client)
    monkeypatch.setattr("app.rag.jsonl_indexing.SLICE_SECONDS", 0)
    monkeypatch.setattr("app.rag.jsonl_indexing.RECORD_BATCH_CHUNKS", 1)
    monkeypatch.setattr("app.rag.parser.MAX_DOCUMENT_CHUNKS", 2)
    monkeypatch.setattr("app.rag.chunking.MAX_DOCUMENT_CHUNKS", 2)
    monkeypatch.setattr("app.rag.parser.MAX_TEXT_CHARACTERS", 100)
    dispatched = []
    monkeypatch.setattr("app.rag.jsonl_indexing.dispatch_document", lambda *a: dispatched.append(a))
    content = "\n".join(json.dumps({"record": i, "text": "content"}) for i in range(8)).encode()
    response = client.post(
        f"/api/v1/knowledge-bases/{base_id}/documents?filename=large.jsonl",
        content=content, headers=headers,
    )
    assert response.status_code == 202
    key = response.json()["id"]
    engine, settings = client.app.state.database, client.app.state.settings
    with Session(engine) as db:
        db.get(Document, key).progress = {
            "jsonl_parser_version": parser_version, "jsonl_offset": 0,
        }
        db.commit()
    for completed in range(1, 9):
        index_document(engine, settings, key, 1)
        with Session(engine) as db:
            doc = db.get(Document, key)
            assert doc.status == ("ready" if completed == 8 else "queued"), doc.error_code
            assert doc.progress["processed_records"] == completed
            assert doc.progress["indexed_chunks"] == completed
            assert doc.index_version == 1
            assert doc.progress["jsonl_parser_version"] == parser_version
            assert doc.progress["stage_seconds"]["embedding"] >= 0
    assert len(dispatched) == 7
    with Session(engine) as db:
        chunks = list(db.scalars(
            select(Chunk).where(Chunk.document_id == key).order_by(Chunk.ordinal),
        ))
        assert [json.loads(row.content.split("\n\n")[-1])["record"] for row in chunks] == list(range(8))
        parents = list(db.scalars(select(ParentChunk).where(ParentChunk.document_id == key)))
        assert len(parents) == 8
        assert db.get(Document, key).progress["processed_bytes"] == len(content)
    assert len(storage[1]) == 8


def test_upload_limits_are_specific_to_jsonl(client, storage, monkeypatch):
    """在小尺寸边界上验证超限、恰好上限，以及其他类型不放宽。"""
    _, headers, base_id = base(client)
    monkeypatch.setattr("app.rag.router.MAX_JSONL_FILE_BYTES", 30)
    monkeypatch.setattr("app.rag.router.MAX_FILE_BYTES", 10)
    path = f"/api/v1/knowledge-bases/{base_id}/documents?filename="
    response = client.post(path + "too-big.jsonl", content=b"x" * 31, headers=headers)
    assert response.status_code == 413
    assert client.post(path + "limit.jsonl", content=b"x" * 30, headers=headers).status_code == 202
    assert client.post(path + "small.txt", content=b"y" * 11, headers=headers).status_code == 413
    assert len(storage[0]) == 1


def test_jsonl_bulk_write_preserves_records_and_stops_after_revocation(
    client, storage, monkeypatch,
):
    """合并写入不合并来源；编码期间删除资料后不能写入向量或发布 ready。"""
    from app.rag.embeddings import Embeddings

    _, headers, base_id = base(client)
    content = b'\n'.join(json.dumps({"record": i}).encode() for i in range(260))
    document = client.post(
        f"/api/v1/knowledge-bases/{base_id}/documents?filename=batch.jsonl",
        content=content, headers=headers,
    ).json()
    engine, settings = client.app.state.database, client.app.state.settings
    encode = Embeddings.encode
    calls = 0

    def revoke(self, texts):
        nonlocal calls
        calls += 1
        result = encode(self, texts)
        if calls == 2:
            client.delete(f"/api/v1/documents/{document['id']}", headers=headers)
        return result

    monkeypatch.setattr(Embeddings, "encode", revoke)
    index_document(engine, settings, document["id"], 1)
    assert calls == 2 and not storage[1]
    with Session(engine) as db:
        assert db.get(Document, document["id"]).status == "deleting"
        assert not list(db.scalars(select(Chunk).where(Chunk.document_id == document["id"])))


def test_recovering_jsonl_cleanup_removes_pending_vectors_and_retries_automatically(
    client, storage, monkeypatch,
):
    """集合不可查询时按主键清理，包含先写向量但 SQL 未提交的块。"""
    from app.rag import indexing
    from app.rag.embeddings import VectorStoreError
    from app.tasks.reliability import sweep

    _, headers, base_id = base(client)
    document = client.post(
        f"/api/v1/knowledge-bases/{base_id}/documents?filename=cleanup.jsonl",
        content=b'{"text":"cleanup"}', headers=headers,
    ).json()
    engine, settings = client.app.state.database, client.app.state.settings
    index_document(engine, settings, document["id"], 1)
    client.delete(f"/api/v1/documents/{document['id']}", headers=headers)
    with Session(engine) as db:
        # 模拟向量写入后进程丢失：父块已提交，子块 SQL 尚未提交。
        for chunk in db.scalars(select(Chunk).where(Chunk.document_id == document["id"])):
            db.delete(chunk)
        db.get(Document, document["id"]).error_code = "document_cleanup_failed"
        db.commit()

    class Index:
        def __init__(self, settings):
            pass

        def remove(self, collection, document_id):
            raise VectorStoreError(106)

        def request(self, operation, payload):
            assert operation == "entities/delete"
            for identifier in json.loads(payload["filter"].removeprefix("id in ")):
                storage[1].pop(identifier, None)
            return {"code": 0}

    monkeypatch.setattr(indexing, "VectorIndex", Index)
    dispatched = []
    sweep(engine, settings, lambda *args: None, lambda *args: dispatched.append(args))
    assert (document["id"], 1, True) in dispatched
    indexing.cleanup_document(engine, settings, document["id"])
    indexing.cleanup_document(engine, settings, document["id"])
    assert not storage[1] and not storage[0]
    with Session(engine) as db:
        doc = db.get(Document, document["id"])
        assert doc.status == "deleted" and doc.error_code is None


def test_cleanup_sql_failure_records_error_and_can_be_retried(client, storage, monkeypatch):
    """SQL flush 失败后必须先回滚，才能保存墓碑错误并支持下一次自动重试。"""
    from app.rag import indexing

    _, headers, base_id = base(client)
    document = client.post(
        f"/api/v1/knowledge-bases/{base_id}/documents?filename=cleanup.txt",
        content=b"cleanup SQL failure", headers=headers,
    ).json()
    engine, settings = client.app.state.database, client.app.state.settings
    index_document(engine, settings, document["id"], 1)
    client.delete(f"/api/v1/documents/{document['id']}", headers=headers)
    original = indexing.clear_graph

    def fail(db, key):
        """制造真实 NOT NULL 约束失败，使 Session 进入必须回滚的状态。"""
        db.add(Chunk(document_id=key, ordinal=9999, content=None, index_version=1))
        db.flush()

    monkeypatch.setattr(indexing, "clear_graph", fail)
    indexing.cleanup_document(engine, settings, document["id"])
    with Session(engine) as db:
        doc = db.get(Document, document["id"])
        assert doc.status == "deleting" and doc.error_code == "document_cleanup_failed"
    monkeypatch.setattr(indexing, "clear_graph", original)
    indexing.cleanup_document(engine, settings, document["id"])
    with Session(engine) as db:
        assert db.get(Document, document["id"]).status == "deleted"


def test_jsonl_larger_than_old_limit_uploads_to_disk_and_downloads(
    client, storage, monkeypatch, tmp_path,
):
    """真实超过 20 MB 的请求落到磁盘，上传结束清理临时文件且原文可完整下载。"""
    _, headers, base_id = base(client)
    files = []

    def temporary_file(**kwargs):
        """把暂存定位到测试目录，并记录文件生命周期。"""
        source = SpooledTemporaryFile(dir=tmp_path, **kwargs)
        files.append(source)
        return source

    monkeypatch.setattr("app.rag.router.SpooledTemporaryFile", temporary_file)
    content = b'{"text":"' + b"x" * (21 * 1024 * 1024) + b'"}\n'
    response = client.post(
        f"/api/v1/knowledge-bases/{base_id}/documents?filename=big.jsonl",
        content=content, headers=headers,
    )
    assert response.status_code == 202
    assert files[0]._rolled and files[0].closed
    assert response.json()["size"] == len(content)
    key = response.json()["id"]
    assert client.get(f"/api/v1/documents/{key}/download").content == content

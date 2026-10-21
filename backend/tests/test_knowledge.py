"""解析边界、跨空间隔离、引用与主动记忆回归；外部存储在单测中注入。"""

import io
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.errors import PdfReadError
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_agents import setup_task
from test_identity import ORIGIN, login

from app.chat.models import Message
from app.memory.context import build_messages
from app.memory.models import ConversationSummary, Memory
from app.rag.embeddings import Embeddings
from app.rag.indexing import cleanup_document, index_document, retrieve
from app.rag.models import Chunk, Document, KnowledgeBase
from app.rag.parser import parse
from app.tasks.execution import Execution
from app.tasks.models import Run

pytest_plugins = ["test_identity"]


def test_pdf_extracted_nul_is_removed_before_chunking(monkeypatch):
    """PDF 字体映射产生的 NUL 不应进入父块、叶块或数据库。"""

    class Page:
        """提供包含不可见控制字符的 PDF 页面。"""

        def get_contents(self):
            """测试页无需原始内容流。"""
            return None

        def extract_text(self):
            """模拟 PDF 字体映射带来的 NUL 字符。"""
            return "before\x00after"

    class Reader:
        """模拟解析成功的单页 PDF。"""

        is_encrypted = False
        pages = [Page()]

        def __init__(self, *args, **kwargs):
            """接受解析器传入的文件和严格模式参数。"""
            pass

    monkeypatch.setattr("app.rag.parser.PdfReader", Reader)
    assert parse(b"%PDF-test", ".pdf") == [{"page": 1, "content": "beforeafter", "ordinal": 1}]


def test_chunk_database_error_is_not_reported_as_vector_failure():
    """分块入库异常与 Milvus 集合异常返回不同的安全原因码。"""
    from sqlalchemy.exc import DataError

    from app.rag.indexing import import_error

    error = DataError("private statement", {}, ValueError("private database error"))
    assert import_error("preparing", error) == "document_chunk_storage_failed"
    assert (
        import_error("preparing", RuntimeError("vector_schema_mismatch"))
        == "vector_schema_mismatch"
    )


def test_index_task_has_independent_timeout_and_safe_reason():
    """长文件使用独立时限，任务超时不会误报为模型不可用。"""
    from celery.exceptions import SoftTimeLimitExceeded

    from app.core.config import Settings
    from app.rag.indexing import import_error
    from app.tasks.worker import import_document

    settings = Settings()
    assert settings.document_index_timeout_seconds > settings.task_timeout_seconds
    assert import_document.soft_time_limit == settings.document_index_timeout_seconds
    assert import_document.time_limit == settings.document_index_timeout_seconds + 10
    assert import_error("embedding", SoftTimeLimitExceeded()) == "document_index_timeout"


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch):
    """替换对象存储与向量索引，提供可观察的隔离测试实现。"""
    objects = {}
    vectors = {}

    class Storage:
        """将对象保存在测试内存中，隔离真实资料存储。"""

        def __init__(self, settings):
            """保存实例所需的依赖与配置，初始化后续执行使用的状态。"""
            pass

        def put(self, key, content):
            """将原始资料写入配置的对象存储位置。"""
            objects[key] = content

        def get(self, key):
            """读取指定对象的内容，交由调用方执行权限与大小检查。"""
            return objects[key]

        def remove(self, key):
            """模拟测试中的资料删除与授权检索操作。"""
            objects.pop(key, None)

    class Index:
        """保存测试索引条目，模拟可控召回与资料删除。"""

        def __init__(self, settings):
            """保存实例所需的依赖与配置，初始化后续执行使用的状态。"""
            pass

        def ensure(self, embedding):
            """检查或创建索引集合，并验证现有向量字段维度。"""
            pass

        def upsert(self, collection, rows):
            """向指定集合批量写入或更新带版本标识的向量条目。"""
            for row in rows:
                vectors[row["id"]] = row

        def remove(self, collection, key):
            """模拟测试中的资料删除与授权检索操作。"""
            for identifier in list(vectors):
                if vectors[identifier]["document_id"] == key:
                    vectors.pop(identifier)

        def search(self, collection, workspace, base, documents, vector):
            """模拟测试中的资料删除与授权检索操作。"""
            rows = [
                {
                    "id": key,
                    "distance": sum(a * b for a, b in zip(row["vector"], vector, strict=True)),
                }
                for key, row in vectors.items()
                if row["account_id"] == workspace
                and row["knowledge_base_id"] == base
                and row["document_id"] in documents
            ]
            return sorted(rows, key=lambda row: row["distance"], reverse=True)

        async def recall(
            self, collection, workspace, base, documents, data, sparse, deadline, guard
        ):
            """隔离模拟两路召回；原生 BM25 的正确性由真实 Milvus 集成验收覆盖。"""
            guard()
            if not sparse:
                return self.search(collection, workspace, base, documents, data)
            return [
                {"id": key, "distance": 1.0}
                for key, row in vectors.items()
                if row["account_id"] == workspace
                and row["knowledge_base_id"] == base
                and documents.get(row["document_id"]) == row["index_version"]
                and any(term in row["text"] for term in data.split())
            ]

    monkeypatch.setattr("app.rag.router.ObjectStorage", Storage)
    monkeypatch.setattr("app.rag.indexing.ObjectStorage", Storage)
    monkeypatch.setattr("app.rag.indexing.VectorIndex", Index)
    monkeypatch.setattr("app.rag.retrieval.VectorIndex", Index)
    return objects, vectors


def base(client):
    """为知识库测试建立有权限的空间与知识库。"""
    identity = login(client)
    headers = {**ORIGIN, "X-CSRF-Token": identity["csrf_token"]}
    space = identity["user"]["id"]
    client.app.state.dispatch_document = lambda *args: None
    result = client.post(f"/api/v1/me/knowledge-bases", json={"name": "测试资料"}, headers=headers)
    assert result.status_code == 201
    return identity, headers, result.json()["id"]


@pytest.mark.parametrize("queue_available", [True, False])
def test_delete_base_revokes_documents_and_cleans_storage(client, storage, queue_available):
    """整库删除立即撤销访问；队列故障也保留可恢复的清理意图。"""
    _, headers, key = base(client)
    doc = client.post(
        f"/api/v1/knowledge-bases/{key}/documents?filename=delete.txt",
        content=b"knowledge base deletion test", headers=headers,
    ).json()
    index_document(client.app.state.database, client.app.state.settings, doc["id"], 1)
    assert storage[0] and storage[1]
    dispatched = []

    def dispatch(*args):
        """记录清理调度，或模拟队列暂时不可用。"""
        if not queue_available:
            raise RuntimeError("queue unavailable")
        dispatched.append(args)

    client.app.state.dispatch_document = dispatch
    assert client.delete(f"/api/v1/knowledge-bases/{key}", headers=headers).status_code == 204
    assert client.get("/api/v1/me/knowledge-bases").json()["items"] == []
    assert client.get(f"/api/v1/knowledge-bases/{key}/documents").status_code == 404
    assert client.post(
        f"/api/v1/knowledge-bases/{key}/search", json={"query": "test"}, headers=headers,
    ).status_code == 404
    assert client.post(
        f"/api/v1/knowledge-bases/{key}/documents?filename=new.txt",
        content=b"new document", headers=headers,
    ).status_code == 404
    assert client.get(f"/api/v1/documents/{doc['id']}/download").status_code == 404
    with Session(client.app.state.database) as db:
        assert db.get(KnowledgeBase, key).deleted
        row = db.get(Document, doc["id"])
        assert row.status == "deleting" and row.lease_token is None
    assert dispatched == ([(doc["id"], 1, True)] if queue_available else [])
    cleanup_document(client.app.state.database, client.app.state.settings, doc["id"])
    assert not storage[0] and not storage[1]


def test_delete_base_requires_owner_and_csrf(client):
    """删除接口拒绝缺少防伪令牌及其他账号的请求。"""
    _, headers, key = base(client)
    path = f"/api/v1/knowledge-bases/{key}"
    assert client.delete(path, headers=ORIGIN).status_code == 403
    other = login(client, "member@example.test")
    assert client.delete(
        path, headers={**ORIGIN, "X-CSRF-Token": other["csrf_token"]},
    ).status_code == 404
    login(client)
    assert len(client.get("/api/v1/me/knowledge-bases").json()["items"]) == 1


@pytest.mark.parametrize(
    "content,suffix",
    [
        (b"", ".txt"),
        (b"\xff", ".md"),
        (b"%PDF-broken", ".pdf"),
        (b"\x00binary", ".txt"),
        (b"%PDF-1.7", ".txt"),
        (b"text", ".exe"),
    ],
)
def test_parse_rejects_invalid_files(content, suffix):
    """验证资料解析拒绝非法编码、类型与超出边界的文件。"""
    with pytest.raises((ValueError, UnicodeError, PdfReadError)):
        parse(content, suffix)


def test_scanned_pdf_requires_ocr():
    """验证无可提取文本的扫描文档明确要求文字识别。"""
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    output = io.BytesIO()
    writer.write(output)
    with pytest.raises(ValueError, match="pdf_requires_ocr"):
        parse(output.getvalue(), ".pdf")


def test_utf8_chunks_keep_overlap_and_positions():
    """验证中文分块保留规定重叠和原始位置。"""
    chunks = parse(("中文资料" * 400).encode(), ".md")
    assert len(chunks) == 3 and chunks[1]["ordinal"] == 2
    assert chunks[0]["content"][-100:] == chunks[1]["content"][:100]


def test_upload_dedup_retry_and_delete_propagation(client: TestClient, storage):
    """验证上传去重、失败重试和删除后的存储清理。"""
    identity, headers, key = base(client)
    path = f"/api/v1/knowledge-bases/{key}/documents?filename=部署说明.md"
    text = "项目使用 Milvus 保存向量，PostgreSQL 保存文档和任务状态。".encode()
    doc = client.post(path, content=text, headers=headers).json()
    assert client.post(path, content=text, headers=headers).json()["id"] == doc["id"]
    assert len(storage[0]) == 1
    engine, settings = client.app.state.database, client.app.state.settings
    index_document(engine, settings, doc["id"], 1)
    index_document(engine, settings, doc["id"], 1)
    with Session(engine) as db:
        row = db.get(Document, doc["id"])
        assert row.status == "ready" and len(db.scalars(select(Chunk)).all()) == 1
        sources = retrieve(
            db,
            settings,
            identity["user"]["id"],
            identity["user"]["id"],
            key,
            "Milvus 保存向量",
        )
        assert sources[0]["document_id"] == doc["id"]
    chunk = sources[0]["chunk_id"]
    assert client.get(f"/api/v1/sources/{chunk}").status_code == 200
    assert client.delete(f"/api/v1/documents/{doc['id']}", headers=headers).status_code == 202
    assert client.get(f"/api/v1/sources/{chunk}").status_code == 404
    with Session(engine) as db:
        assert (
            retrieve(db, settings, identity["user"]["id"], identity["user"]["id"], key, "Milvus")
            == []
        )
    cleanup_document(engine, settings, doc["id"])
    cleanup_document(engine, settings, doc["id"])
    assert not storage[0] and not storage[1]
    new = client.post(path, content=text, headers=headers).json()
    assert new["id"] != doc["id"]


def test_knowledge_read_write_scope_and_filename(client: TestClient, storage):
    """验证知识库读写权限边界和文件名路径限制。"""
    _, headers, key = base(client)
    path = f"/api/v1/knowledge-bases/{key}/documents"
    assert (
        client.post(path + "?filename=../x.txt", content=b"a", headers=headers).status_code == 422
    )
    doc = client.post(path + "?filename=x.txt", content=b"data", headers=headers).json()
    other = login(client, "other@example.test")
    other_headers = {**ORIGIN, "X-CSRF-Token": other["csrf_token"]}
    assert client.get(path).status_code == 404
    assert client.get(f"/api/v1/documents/{doc['id']}/download").status_code == 404
    assert client.delete(f"/api/v1/documents/{doc['id']}", headers=other_headers).status_code == 404
    member = login(client, "member@example.test")
    member_headers = {**ORIGIN, "X-CSRF-Token": member["csrf_token"]}
    assert client.get(path).status_code == 404
    assert (
        client.post(path + "?filename=x.txt", content=b"data", headers=member_headers).status_code
        == 404
    )


def test_index_failure_is_retryable_and_never_searchable(client: TestClient, storage):
    """验证索引失败可重试，失败资料不会进入检索。"""
    _, headers, key = base(client)
    doc = client.post(
        f"/api/v1/knowledge-bases/{key}/documents?filename=bad.pdf",
        content=b"%PDF-broken",
        headers=headers,
    ).json()
    index_document(client.app.state.database, client.app.state.settings, doc["id"], 1)
    rows = client.get(f"/api/v1/knowledge-bases/{key}/documents").json()["items"]
    assert rows[0]["status"] == "failed"
    assert rows[0]["progress"]["failed_stage"] == "parsing"
    retried = client.post(f"/api/v1/documents/{doc['id']}/retry", headers=headers).json()
    assert retried["index_version"] == 2 and retried["status"] == "queued"
    assert retried["progress"] == {}
    index_document(client.app.state.database, client.app.state.settings, doc["id"], 1)
    assert (
        client.get(f"/api/v1/knowledge-bases/{key}/documents").json()["items"][0]["status"]
        == "queued"
    )


@pytest.mark.parametrize(
    "stage,target,code",
    [
        ("reading", "ObjectStorage.get", "document_storage_read_failed"),
        ("parsing", "parse_file", "document_parse_failed"),
        ("preparing", "VectorIndex.ensure", "vector_prepare_failed"),
        ("embedding", "Embeddings.encode", "embedding_service_failed"),
        ("writing", "VectorIndex.upsert", "vector_write_failed"),
    ],
)
def test_import_reports_exact_failed_step(client, storage, monkeypatch, stage, target, code):
    """验证导入出错时记录准确的失败步骤。"""
    _, headers, key = base(client)
    doc = client.post(
        f"/api/v1/knowledge-bases/{key}/documents?filename=progress.md",
        content="用于检查失败步骤的文档。".encode(),
        headers=headers,
    ).json()

    def fail(*args, **kwargs):
        """在指定操作处注入失败，验证错误状态与恢复边界。"""
        raise RuntimeError("外部服务敏感详情不应出现在响应里")

    monkeypatch.setattr(f"app.rag.indexing.{target}", fail)
    index_document(client.app.state.database, client.app.state.settings, doc["id"], 1)
    result = client.get(f"/api/v1/knowledge-bases/{key}/documents").json()["items"][0]
    assert result["status"] == "failed"
    assert result["progress"]["failed_stage"] == stage
    assert result["error_code"] == code
    assert "敏感" not in str(result)


@pytest.mark.parametrize("failure", [None, "embedding", "writing"])
def test_batch_progress_commits_before_call_and_preserves_counts(
    client, storage, monkeypatch, failure
):
    """验证外部调用前提交批次进度，失败后保留已完成计数。"""
    _, headers, key = base(client)
    doc = client.post(
        f"/api/v1/knowledge-bases/{key}/documents?filename=batches.md",
        content=("多批次中文资料，用于验证编码与写入进度。" * 700).encode(),
        headers=headers,
    ).json()
    engine, settings = client.app.state.database, client.app.state.settings
    original_encode = Embeddings.encode
    # 存储测试替身的实际写入函数，保持成功批次的真实行为。
    from app.rag import indexing as knowledge

    original_upsert = knowledge.VectorIndex.upsert
    observed = []

    def observe(stage):
        """读取已经提交的资料进度，验证调用前状态对外可见。"""
        with Session(engine) as db:
            progress = dict(db.get(Document, doc["id"]).progress)
        assert progress["stage"] == stage
        observed.append(progress)
        return progress

    def encode(self, texts):
        """提供测试嵌入输出或指定失败，观察分批处理进度。"""
        progress = observe("embedding")
        if failure == "embedding" and progress["batch_number"] == 2:
            raise httpx.ReadTimeout("模型内部地址不应公开")
        return original_encode(self, texts)

    def write(self, collection, rows):
        """注入向量写入行为，验证失败位置与已完成数量。"""
        progress = observe("writing")
        if failure == "writing" and progress["batch_number"] == 2:
            raise RuntimeError("连接失败")
        return original_upsert(self, collection, rows)

    monkeypatch.setattr(Embeddings, "encode", encode)
    monkeypatch.setattr(knowledge.VectorIndex, "upsert", write)
    index_document(engine, settings, doc["id"], 1)
    result = client.get(f"/api/v1/knowledge-bases/{key}/documents").json()["items"][0]
    progress = result["progress"]
    assert progress["total_chunks"] > 16
    assert observed[0]["embedded_chunks"] == observed[0]["indexed_chunks"] == 0
    assert observed[1]["embedded_chunks"] == 16 and observed[1]["indexed_chunks"] == 0
    if failure:
        assert result["status"] == "failed" and progress["failed_stage"] == failure
        assert progress["indexed_chunks"] == 16
        assert progress["embedded_chunks"] == (
            16 if failure == "embedding" else progress["total_chunks"]
        )
        assert result["error_code"] == (
            "embedding_timeout" if failure == "embedding" else "vector_write_failed"
        )
        retried = client.post(f"/api/v1/documents/{doc['id']}/retry", headers=headers).json()
        assert retried["progress"] == {} and retried["index_version"] == 2
        index_document(engine, settings, doc["id"], 1)
        assert (
            client.get(f"/api/v1/knowledge-bases/{key}/documents").json()["items"][0]["progress"]
            == {}
        )
    else:
        assert result["status"] == "ready" and progress["stage"] == "ready"
        assert progress["embedded_chunks"] == progress["indexed_chunks"] == progress["total_chunks"]


def test_memory_confirmation_isolation_disable_and_delete(client: TestClient):
    """验证记忆必须主动确认、按用户隔离，并可关闭和删除。"""
    identity = login(client)
    key = identity["user"]["id"]
    headers = {**ORIGIN, "X-CSRF-Token": identity["csrf_token"]}
    path = "/api/v1/me"
    assert (
        client.post(path + "/memories", json={"content": "默认中文"}, headers=headers).status_code
        == 422
    )
    assert (
        client.post(
            path + "/memories",
            json={"content": "我的密码是 abc", "confirmed": True},
            headers=headers,
        ).status_code
        == 422
    )
    row = client.post(
        path + "/memories", json={"content": "默认中文", "confirmed": True}, headers=headers
    ).json()
    member = login(client, "member@example.test")
    assert client.get(path + "/memories").json()["items"] == []
    assert (
        client.delete(
            f"/api/v1/memories/{row['id']}",
            headers={**ORIGIN, "X-CSRF-Token": member["csrf_token"]},
        ).status_code
        == 404
    )
    identity = login(client)
    headers["X-CSRF-Token"] = identity["csrf_token"]
    client.patch(path + "/memory-preference", json={"enabled": False}, headers=headers)
    assert client.get(path + "/memories").json() == {"enabled": False, "items": []}
    assert (
        client.post(
            path + "/memories", json={"content": "默认中文", "confirmed": True}, headers=headers
        ).status_code
        == 409
    )
    client.patch(path + "/memory-preference", json={"enabled": True}, headers=headers)
    assert client.delete(f"/api/v1/memories/{row['id']}", headers=headers).status_code == 204
    assert client.get(path + "/memories").json()["items"] == []


def test_long_context_summary_and_memory_injection(client: TestClient):
    """验证较长对话生成有界摘要，且只注入允许使用的记忆。"""
    _, _, run, _ = setup_task(client)
    with Session(client.app.state.database) as db:
        row = db.get(Run, run["id"])
        first = db.scalar(select(Message))
        first.content = "重要：必须使用中文，数据库只能使用 Milvus。"
        for index in range(2, 60):
            db.add(
                Message(
                    conversation_id=row.conversation_id,
                    sequence=index,
                    role="user",
                    content="讨论过程" * 100,
                )
            )
        memory = Memory(account_id=row.account_id, user_id=row.owner_id, content="示例优先 Python")
        db.add(memory)
        db.flush()
        result = build_messages(db, row)
        summary = db.get(ConversationSummary, row.conversation_id)
        assert first.id in summary.source_ids and "只能使用 Milvus" in summary.content
        assert len(str(result).encode()) < 24000
        assert "示例优先 Python" in str(result)


def test_rag_citations_and_no_evidence_answer(client: TestClient, storage):
    """验证正文隐藏引用索引但保留真实来源，无证据时不使用通用知识。"""
    _, headers, key = base(client)
    doc = client.post(
        f"/api/v1/knowledge-bases/{key}/documents?filename=架构.md",
        content="项目使用 Milvus 保存向量。".encode(),
        headers=headers,
    ).json()
    index_document(client.app.state.database, client.app.state.settings, doc["id"], 1)
    headers, body, original, _ = setup_task(client)
    client.post(f"/api/v1/runs/{original['id']}/cancel", headers=headers)
    headers["Idempotency-Key"] = str(uuid4())
    result = client.post(
        "/api/v1/runs",
        json={**body, "knowledge_base_id": key, "message": "Milvus 保存向量"},
        headers=headers,
    )
    assert result.status_code == 202
    Execution(client.app.state.database, client.app.state.settings, result.json()["id"]).execute()
    messages = client.get(f"/api/v1/conversations/{body['conversation_id']}/messages").json()[
        "items"
    ]
    assert messages[-1]["sources"][0]["document_id"] == doc["id"]
    assert "Milvus" in messages[-1]["content"]
    assert "[S1]" not in messages[-1]["content"]
    assert messages[-1]["sources"][0]["label"] == "S1"
    assert result.json()["knowledge_base_id"] == key
    client.delete(f"/api/v1/documents/{doc['id']}", headers=headers)
    headers["Idempotency-Key"] = str(uuid4())
    task = client.post(
        "/api/v1/runs", json={**body, "knowledge_base_id": key}, headers=headers
    ).json()
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute()
    final = client.get(f"/api/v1/conversations/{body['conversation_id']}/messages").json()["items"][
        -1
    ]
    assert final["content"] == "没有相关知识" and final["sources"] == []

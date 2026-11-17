"""验证自动类型、独立父块、token 上限与真实上传重试链路。"""

from collections import defaultdict

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_knowledge import base
from tokenizers import Tokenizer, models, pre_tokenizers, processors

from app.rag.chunking import detect_profile, requested_strategy, strategy_version, windows
from app.rag.indexing import index_document
from app.rag.models import Chunk, Document, ParentChunk
from app.rag.parser import parse
from app.rag.retrieval import document_scope, materialize

pytest_plugins = ["test_identity", "test_knowledge"]


@pytest.fixture
def tokenizer_path(tmp_path, monkeypatch):
    """每个字符一个 token，保留特殊 token，子进程也读取同一隔离文件。"""
    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0, "[CLS]": 1, "[SEP]": 2}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Split("", behavior="isolated")
    tokenizer.post_processor = processors.TemplateProcessing(
        single="[CLS] $A [SEP]", special_tokens=[("[CLS]", 1), ("[SEP]", 2)]
    )
    tokenizer.save(str(tmp_path / "tokenizer.json"))
    monkeypatch.setenv("BGE_MODEL_PATH", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_MODEL", "BAAI/bge-m3")
    return tokenizer


@pytest.mark.parametrize(
    "text,expected",
    [
        ("短内容", "short"),
        ("长" * 2001, "long"),
        ("超长" * 10000, "ultra"),
        ("English text " * 250, "tokens"),
        ("## A\n内容\n## B\n内容", "structured"),
        ("【成分】\n甲\n【禁忌】\n乙", "structured"),
        ("1. 甲\n2. 乙\n3. 丙", "structured"),
        ("```\n## A\n## B\n```\n普通说明", "short"),
    ],
    ids=["short", "long", "ultra", "english", "markdown", "entries", "list", "code"],
)
def test_detection(text, expected):
    """可解释规则覆盖边界、语言和显式结构，代码示例不冒充章节。"""
    assert detect_profile(text) == expected


@pytest.mark.parametrize(
    "profile,parent_size,child_size",
    [
        ("short", 500, 100),
        ("long", 1000, 200),
        ("tokens", 1024, 256),
        ("ultra", 4096, 400),
    ],
)
def test_parent_first_limits_and_complete_text(tokenizer_path, profile, parent_size, child_size):
    """每块独立满足上限，父块拼回原文，每个父块可拥有超过三个子块。"""
    text = "甲" * 5200
    parts = parse(text.encode(), ".txt", profile)
    groups = defaultdict(list)
    for part in parts:
        groups[part["parent_group"]].append(part)
        length = (
            len(tokenizer_path.encode(part["content"]).ids)
            if profile in {"tokens", "ultra"}
            else len(part["content"])
        )
        assert length <= child_size
    parents = [members[0]["parent_content"] for members in groups.values()]
    assert "".join(parents) == text
    assert max(len(members) for members in groups.values()) > 3
    for parent in parents:
        length = (
            len(tokenizer_path.encode(parent).ids)
            if profile in {"tokens", "ultra"}
            else len(parent)
        )
        assert length <= parent_size
    for members in groups.values():
        assert all("parent_content" not in member for member in members[1:])


def test_structure_and_manual_override(tokenizer_path):
    """结构方案保留主题与句子；手动覆盖不被自动检测改回。"""
    text = "## 成分\n甲成分。乙成分。\n## 禁忌\n丙禁忌。丁禁忌。"
    automatic = parse(text.encode(), ".md", "auto")
    assert {part["profile"] for part in automatic} == {"structured"}
    assert all(not ("成分" in part["content"] and "禁忌" in part["content"]) for part in automatic)
    assert any(part["content"] == "乙成分。" for part in automatic)
    assert {part["profile"] for part in parse(text.encode(), ".md", "long")} == {"long"}


def test_missing_tokenizer_is_explicit(tmp_path, monkeypatch):
    """缺失 tokenizer 不能静默按字符计数或下载模型。"""
    monkeypatch.setenv("BGE_MODEL_PATH", str(tmp_path / "missing.json"))
    with pytest.raises(ValueError, match="chunk_tokenizer_unavailable"):
        parse("内容".encode(), ".txt", "tokens")


def test_token_windows_only_encode_current_prefix(tokenizer_path):
    """长文 token 切分的编码工作量随文本长度增长，且无内容截断。"""
    sizes = []

    class CountingTokenizer:
        """记录真实分词器每次收到的字符数。"""

        def num_special_tokens_to_add(self, pair):
            """保留真实模型特殊 token 开销。"""
            return tokenizer_path.num_special_tokens_to_add(pair)

        def encode(self, text, **kwargs):
            """转交真实分词并记录工作量。"""
            sizes.append(len(text))
            return tokenizer_path.encode(text, **kwargs)

    text = "甲" * 100_000
    spans = list(windows(text, 4096, "tokens", tokenizer=CountingTokenizer()))
    assert "".join(text[begin:end] for begin, end in spans) == text
    assert max(sizes) <= 8192
    assert sum(sizes) < len(text) * 4


@pytest.mark.parametrize("structured", [False, True])
def test_long_automatic_token_profiles_keep_full_text(tokenizer_path, structured):
    """默认自动模式可处理超过旧上限的超长及章节文档，父块完整覆盖原文。"""
    text = "指南正文" * 150_000
    if structured:
        text = "## 前半部分\n" + text[:300_000] + "\n## 后半部分\n" + text[300_000:]
    parts = parse(text.encode(), ".md", "auto")
    assert len(parts) > 1000
    assert {part["profile"] for part in parts} == {"structured" if structured else "ultra"}
    parents = [part["parent_content"] for part in parts if "parent_content" in part]
    assert "".join(parents).replace("\n", "") == text.replace("\n", "")


def test_upload_retry_and_single_child_expands_full_parent(client, storage):
    """真实导入保存父子关系，一次命中即可返回父块，重试保留自动模式。"""
    identity, headers, base_id = base(client)
    text = "完整内容" * 100
    path = f"/api/v1/knowledge-bases/{base_id}/documents?filename=profile.txt"
    response = client.post(path, content=text.encode(), headers=headers)
    assert response.status_code == 202 and response.json()["chunk_strategy"] == "pc-auto-auto-v1"
    key = response.json()["id"]
    engine, settings = client.app.state.database, client.app.state.settings
    index_document(engine, settings, key, 1)
    with Session(engine) as db:
        document = db.get(Document, key)
        assert document.status == "ready" and document.chunk_strategy == "pc-auto-short-v1"
        parents = list(db.scalars(select(ParentChunk).where(ParentChunk.document_id == key)))
        assert len(parents) == 1 and parents[0].child_count > 3
        child = db.scalar(select(Chunk).where(Chunk.document_id == key))
        user = identity["user"]["id"]
        scope = document_scope(db, settings, user, user, base_id)
        sources = materialize(
            db, settings, user, user, base_id, scope, [{"id": child.id, "rrf_score": 0.03}]
        )
        assert len(sources) == 1 and sources[0]["merged"]
        assert sources[0]["content"] == text
        assert len(sources[0]["child_ids"]) == parents[0].child_count
    assert client.post(path, content=text.encode(), headers=headers).json()["duplicate"]
    assert (
        client.post(
            path + "&chunk_strategy=long", content=text.encode(), headers=headers
        ).status_code
        == 409
    )
    assert client.post(f"/api/v1/documents/{key}/retry", headers=headers).status_code == 200
    index_document(engine, settings, key, 2)
    with Session(engine) as db:
        assert db.get(Document, key).chunk_strategy == "pc-auto-short-v1"
    override = client.post(f"/api/v1/documents/{key}/retry?chunk_strategy=long", headers=headers)
    assert override.status_code == 200 and override.json()["index_version"] == 3
    index_document(engine, settings, key, 3)
    with Session(engine) as db:
        assert db.get(Document, key).status == "ready"
        assert db.get(Document, key).chunk_strategy == "pc-long-long-v1"


def test_tokenizer_error_survives_import_chain(client, storage, tmp_path, monkeypatch):
    """子进程的 tokenizer 原因码完整传回 UI，不被误报成通用解析失败。"""
    monkeypatch.setenv("BGE_MODEL_PATH", str(tmp_path / "missing"))
    _, headers, base_id = base(client)
    response = client.post(
        f"/api/v1/knowledge-bases/{base_id}/documents?filename=token.txt&chunk_strategy=tokens",
        content="测试原文".encode(),
        headers=headers,
    )
    key = response.json()["id"]
    index_document(client.app.state.database, client.app.state.settings, key, 1)
    with Session(client.app.state.database) as db:
        assert db.get(Document, key).status == "failed"
        assert db.get(Document, key).error_code == "chunk_tokenizer_unavailable"


@pytest.mark.parametrize(
    "mode", ["auto", "short", "long", "tokens", "structured", "ultra", "fixed", "markdown_sections"]
)
def test_version_roundtrip(mode):
    """选择和识别结果持久化后仍能恢复最初请求。"""
    assert requested_strategy(strategy_version(mode, "short")) == mode


def test_long_document_indexes_all_batches(client, storage, monkeypatch):
    """超过旧文本与子块上限后，真实入库链路将全部批次发布为 ready。"""
    _, headers, base_id = base(client)
    text = "指南正文" * 150_000 + "文档末尾标记"
    sizes = []

    def encode(self, texts):
        """隔离模型耗时，记录批次容量并提供契约一致的向量。"""
        sizes.append(len(texts))
        return [[1.0] + [0.0] * (self.settings.embedding_dimension - 1) for _ in texts]

    monkeypatch.setattr("app.rag.indexing.Embeddings.encode", encode)
    response = client.post(
        f"/api/v1/knowledge-bases/{base_id}/documents?filename=long.txt&chunk_strategy=long",
        content=text.encode(), headers=headers,
    )
    assert response.status_code == 202
    key = response.json()["id"]
    index_document(client.app.state.database, client.app.state.settings, key, 1)
    with Session(client.app.state.database) as db:
        document = db.get(Document, key)
        assert document.status == "ready", document.error_code
        count = document.progress["total_chunks"]
        assert count > 1000
        assert document.progress["indexed_chunks"] == count == sum(sizes)
        assert document.progress["embedded_chunks"] == count
        assert max(sizes) <= 16
        chunks = list(db.scalars(
            select(Chunk).where(Chunk.document_id == key).order_by(Chunk.ordinal),
        ))
        assert len(chunks) == count
        assert chunks[-1].content.endswith("文档末尾标记")
        parents = list(db.scalars(
            select(ParentChunk).where(ParentChunk.document_id == key).order_by(ParentChunk.ordinal),
        ))
        assert "".join(parent.content for parent in parents) == text
    assert len(storage[1]) == count

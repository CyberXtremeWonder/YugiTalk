"""长文档跨过原字符、页数、子块和进程输出限制的回归。"""

import io

import pytest
from pypdf import PdfReader, PdfWriter

from app.rag.indexing import import_error, parse_file
from app.rag.parser import parse


def test_long_text_survives_real_parser_subprocess():
    """真实子进程传回超过旧 8 MB 上限的中文父子分块，保留全文。"""
    text = "指南正文" * 250_000 + "文档末尾标记"
    parts = parse_file(text.encode(), ".txt", "long")
    assert len(parts) > 1000
    assert "".join(part["parent_content"] for part in parts if "parent_content" in part) == text
    assert parts[-1]["content"].endswith("文档末尾标记")
    assert [part["ordinal"] for part in parts] == list(range(1, len(parts) + 1))


def test_pdf_preflight_accepts_more_than_200_pages(monkeypatch):
    """PDF 前置检查和 Docling 使用相同的新页数边界。"""
    writer = PdfWriter()
    for _ in range(201):
        writer.add_blank_page(width=72, height=72)
    content = io.BytesIO()
    writer.write(content)
    monkeypatch.setattr(
        "app.rag.parser.document_pages", lambda *args: [(201, "最后一页")],
    )
    assert parse(content.getvalue(), ".pdf") == [
        {"page": 201, "content": "最后一页", "ordinal": 1},
    ]


def test_text_limit_is_reported_as_size_error():
    """超出新字符上限时给出准确原因，而非提示 UTF-8 编码错误。"""
    with pytest.raises(ValueError, match="text_too_large"):
        parse(b"a" * 5_000_001, ".txt")


@pytest.mark.parametrize("strategy", ["fixed", "long"])
def test_chunk_limit_is_still_enforced(monkeypatch, strategy):
    """新旧分块方式都保留明确的子块资源边界。"""
    monkeypatch.setattr("app.rag.parser.MAX_DOCUMENT_CHUNKS", 2)
    monkeypatch.setattr("app.rag.chunking.MAX_DOCUMENT_CHUNKS", 2)
    with pytest.raises(ValueError, match="too_many_chunks"):
        parse(("指南" * 1000).encode(), ".txt", strategy)


def test_response_size_error_survives_import_chain():
    """Docling 响应超限原因码可安全地展示到页面。"""
    assert import_error("parsing", ValueError("docling_response_too_large")) == (
        "docling_response_too_large"
    )


@pytest.mark.parametrize("password", ["", "secret"])
def test_pdf_encryption_distinguishes_empty_open_password(monkeypatch, password):
    """空打开密码的加密 PDF 可导入；真正需要密码的文件给出独立原因。"""
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_metadata({"/Title": "Guide"})
    writer.encrypt(password, owner_password="owner-secret")
    output = io.BytesIO()
    writer.write(output)
    original = output.getvalue()
    calls = []

    def convert(content, suffix):
        """验证传给 Docling 的副本已解密，页码和元数据仍保留。"""
        reader = PdfReader(io.BytesIO(content))
        assert not reader.is_encrypted
        assert len(reader.pages) == 1
        assert reader.metadata.title == "Guide"
        calls.append(suffix)
        return [(1, "指南内容")]

    monkeypatch.setattr("app.rag.parser.document_pages", convert)
    if password:
        with pytest.raises(ValueError, match="pdf_password_required"):
            parse(original, ".pdf")
        assert calls == []
    else:
        assert parse(original, ".pdf") == [
            {"page": 1, "content": "指南内容", "ordinal": 1},
        ]
        assert calls == [".pdf"]
    assert PdfReader(io.BytesIO(original)).is_encrypted


def test_pdf_page_limit_has_independent_error(monkeypatch):
    """页数超限不再被提示成需要密码。"""
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    output = io.BytesIO()
    writer.write(output)
    monkeypatch.setattr("app.rag.parser.MAX_DOCUMENT_PAGES", 0)
    with pytest.raises(ValueError, match="pdf_too_many_pages"):
        parse(output.getvalue(), ".pdf")


@pytest.mark.parametrize("code", ["pdf_password_required", "pdf_too_many_pages"])
def test_pdf_error_survives_import_chain(code):
    """独立 PDF 原因码完整传递到索引错误状态。"""
    assert import_error("parsing", ValueError(code)) == code

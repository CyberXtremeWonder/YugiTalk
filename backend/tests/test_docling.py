"""本地 Docling 的完整转换、来源页码及服务故障边界。"""

import httpx
import pytest

from app.rag.docling import PAGE_BREAK, document_pages


def install_response(monkeypatch, payload, status=200):
    """注入内存 HTTP 传输层，验证请求契约且不联系真实模型。"""
    original = httpx.Client
    requests = []

    def handle(request):
        """保存发给解析器的请求，返回指定转换状态。"""
        requests.append(request)
        return httpx.Response(status, json=payload)

    monkeypatch.setenv("DOCLING_URL", "http://docling.test:5001")
    monkeypatch.setattr(
        "app.rag.docling.httpx.Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    return requests


def test_docling_requests_ocr_and_tables_preserving_page_numbers(monkeypatch):
    """OCR 与表格明确启用，原文页码来自服务返回值而非猜测。"""
    calls = install_response(monkeypatch, {
        "status": "success", "document": {
            "md_content": f"第一页\x00{PAGE_BREAK}| 姓名 | 部门 |\n| 张三 | 研发 |",
            "json_content": {"pages": {"1": {}, "2": {}}},
        },
    })
    pages = document_pages(b"test image", ".png")
    assert pages == [(1, "第一页"), (2, "| 姓名 | 部门 |\n| 张三 | 研发 |")]
    request = calls[0]
    assert str(request.url) == "http://docling.test:5001/v1/convert/file"
    for field, value in [
        ("do_ocr", "true"), ("do_table_structure", "true"), ("table_mode", "accurate"),
    ]:
        assert f'name="{field}"\r\n\r\n{value}'.encode() in request.content
    assert b'filename="document.png"' in request.content
    assert "authorization" not in request.headers


@pytest.mark.parametrize("status", ["failure", "partial_success", "skipped"])
def test_docling_rejects_incomplete_conversion(monkeypatch, status):
    """残缺转换不能以成功文档进入索引或图谱。"""
    install_response(monkeypatch, {"status": status, "document": {"md_content": "partial"}})
    with pytest.raises(ValueError, match="docling_conversion_failed"):
        document_pages(b"test", ".png")


@pytest.mark.parametrize(
    "status,code", [(503, "docling_unavailable"), (422, "docling_conversion_failed")],
)
def test_docling_http_errors_are_actionable(monkeypatch, status, code):
    """服务不可用与文件转换失败使用可区分的公开错误码。"""
    install_response(monkeypatch, {}, status)
    with pytest.raises(ValueError, match=code):
        document_pages(b"test", ".png")


def test_docling_does_not_invent_page_numbers(monkeypatch):
    """页边界不完整时保留全文，不将内容标成错误页码。"""
    install_response(monkeypatch, {"status": "success", "document": {
        "md_content": "full document", "json_content": {"pages": {"1": {}, "2": {}}},
    }})
    assert document_pages(b"test", ".png") == [(None, "full document")]


def test_docling_unexpected_response_is_not_a_document(monkeypatch):
    """返回成功但缺失原文时明确失败。"""
    install_response(monkeypatch, {"status": "success", "document": {"md_content": None}})
    with pytest.raises(ValueError, match="docling_response_invalid"):
        document_pages(b"test", ".png")


def test_docling_excel_retains_sheet_column_and_row_context(monkeypatch):
    """Docling 默认 Markdown 不含表名时，使用其 JSON 表格的真实来源信息。"""
    install_response(monkeypatch, {"status": "success", "document": {
        "md_content": "| 库存 |\n| 0 |", "json_content": {
            "pages": {"2": {}},
            "groups": [{"label": "sheet", "name": "仓库", "children": [{"$ref": "#/tables/0"}]}],
            "tables": [{"self_ref": "#/tables/0", "prov": [{"page_no": 2}], "data": {
                "grid": [[{"text": "库存"}], [{"text": "0"}]],
            }}],
        },
    }})
    pages = document_pages(b"test workbook", ".xls")
    assert pages == [(2, "工作表：仓库；表头：库存\n工作表：仓库；表内第2行：库存=0")]


def test_docling_accepts_long_document_and_large_structured_response(monkeypatch):
    """超过旧字符、页数和响应限制时，仍保留完整文本与真实页号。"""
    texts = ["指南内容" * 500] * 300
    install_response(monkeypatch, {"status": "success", "document": {
        "md_content": PAGE_BREAK.join(texts),
        "json_content": {
            "pages": {str(i): {} for i in range(1, 301)},
            "metadata": "x" * (17 * 1024 * 1024),
        },
    }})
    pages = document_pages(b"test pdf", ".pdf")
    assert pages == list(enumerate(texts, 1))


@pytest.mark.parametrize(
    "limit,value,code",
    [
        ("MAX_TEXT_CHARACTERS", 3, "text_too_large"),
        ("MAX_DOCUMENT_PAGES", 0, "pdf_too_many_pages"),
        ("MAX_DOCLING_RESPONSE_BYTES", 10, "docling_response_too_large"),
    ],
)
def test_docling_resource_limits_remain_enforced(monkeypatch, limit, value, code):
    """资源上限仍有效，响应过大不会误报为文本字符超限。"""
    monkeypatch.setattr(f"app.rag.docling.{limit}", value)
    install_response(monkeypatch, {"status": "success", "document": {
        "md_content": "long text", "json_content": {"pages": {"1": {}}},
    }})
    with pytest.raises(ValueError, match=code):
        document_pages(b"test", ".pdf")

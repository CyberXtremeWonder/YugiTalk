"""Office 文档经真实上传、受限解析、索引、检索及图谱发布的回归。"""

import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zipfile import ZIP_DEFLATED, ZipFile

import openpyxl
import pytest
import xlwt
from docx import Document as WordDocument
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_knowledge import base
from test_knowledge_graph import build, extraction

from app.rag.indexing import index_document, parse_file, retrieve
from app.rag.models import Chunk, Document
from app.rag.parser import parse

pytest_plugins = ["test_identity", "test_knowledge"]


@pytest.fixture(autouse=True)
def docling_service(monkeypatch):
    """模拟 Docling REST 契约，实际解析引擎另用容器样本验收。"""
    endpoint = os.environ.get("DOCLING_INTEGRATION_URL")
    if endpoint:
        monkeypatch.setenv("DOCLING_URL", endpoint)
        yield
        return
    class Handler(BaseHTTPRequestHandler):
        """返回结构化转换响应，允许真实解析子进程访问隔离测试服务。"""

        def do_POST(self):
            """验证应用明确请求 OCR、表格结构与完整 Markdown 结果。"""
            body = self.rfile.read(int(self.headers["Content-Length"]))
            assert self.path == "/v1/convert/file"
            if b"this is not an office file" in body:
                self.send_response(400)
                self.end_headers()
                return
            text = (
                "支付服务使用DB1。\n负责人 | 张三\n表格之后的说明。\n"
                "系统架构\n| 数量 | 启用 |\n| 0 | False |"
            )
            payload = {"status": "success", "document": {
                "md_content": text, "json_content": {"pages": {}},
            }}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *args):
            """测试不输出请求正文或业务资料。"""
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("DOCLING_URL", f"http://127.0.0.1:{server.server_port}")
    yield
    server.shutdown()
    server.server_close()
    thread.join()


def office_file(suffix):
    """生成可核验正文和表格，避免依赖用户资料或网络下载。"""
    output = io.BytesIO()
    if suffix == ".docx":
        document = WordDocument()
        document.add_paragraph("支付服务使用DB1。")
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "负责人"
        table.cell(0, 1).text = "张三"
        document.add_paragraph("表格之后的说明。")
        document.save(output)
    elif suffix == ".xlsx":
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "系统架构"
        sheet.append(["说明", "数量", "启用"])
        sheet.append(["支付服务使用DB1。", 0, False])
        book.create_sheet("人员").append(["负责人", "张三"])
        book.save(output)
    else:
        book = xlwt.Workbook()
        sheet = book.add_sheet("系统架构")
        for column, value in enumerate(["说明", "数量", "启用"]):
            sheet.write(0, column, value)
        for column, value in enumerate(["支付服务使用DB1。", 0, False]):
            sheet.write(1, column, value)
        book.add_sheet("人员").write(0, 0, "张三")
        book.save(output)
    return output.getvalue()


@pytest.mark.parametrize("suffix", [".xlsx", ".xls", ".docx"])
def test_office_upload_retrieval_and_graph(client, storage, monkeypatch, suffix):
    """Office 提取的原文能在同一流程中索引、检索并构建有依据的图谱。"""
    identity, headers, key = base(client)
    content = office_file(suffix)
    response = client.post(
        f"/api/v1/knowledge-bases/{key}/documents?filename=资料{suffix}",
        content=content, headers=headers,
    )
    assert response.status_code == 202
    doc_id = response.json()["id"]
    engine, settings = client.app.state.database, client.app.state.settings
    index_document(engine, settings, doc_id, 1)
    with Session(engine) as db:
        assert db.get(Document, doc_id).status == "ready"
        text = "\n".join(row.content for row in db.scalars(select(Chunk)))
        assert "张三" in text
        if suffix == ".docx":
            assert text.index("支付服务") < text.index("负责人") < text.index("表格之后")
        else:
            assert "系统架构" in text and "数量" in text and "0" in text and "启用" in text
        owner = identity["user"]["id"]
        assert retrieve(db, settings, owner, owner, key, "支付服务使用DB1")
    build(client, monkeypatch, doc_id, extraction())
    result = client.get(f"/api/v1/documents/{doc_id}/graph").json()
    assert result["status"] == "ready"
    assert result["facts"][0]["quote"] == "支付服务使用DB1。"
    assert client.get(f"/api/v1/documents/{doc_id}/download").content == content


@pytest.mark.parametrize("suffix", [".xlsx", ".xls", ".docx"])
def test_invalid_office_is_reported_by_parser_subprocess(suffix):
    """损坏或伪装的 Office 文件返回可展示的错误，而非 Worker 异常。"""
    with pytest.raises(ValueError, match="invalid_office|docling_conversion_failed"):
        parse_file(b"this is not an office file", suffix)


def test_office_expansion_is_bounded():
    """小体积压缩包不能使解析器展开无界数据。"""
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"x" * (32 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="office_expanded_too_large"):
        parse(output.getvalue(), ".docx")


def test_legacy_word_uses_docling():
    """旧版 Word 同样提交给 Docling，不保留旧的 antiword 路径。"""
    chunks = parse(bytes.fromhex("D0CF11E0A1B11AE1") + b"test", ".doc")
    assert "支付服务使用DB1。" in chunks[0]["content"]

"""工作空间文本读取的资源边界回归，不访问实际用户目录。"""

import io
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from docx import Document
from docx.oxml import OxmlElement
from pydantic import ValidationError

from app.providers.client import ProviderFailure
from app.tools.builtin import files
from app.tools.builtin.files import MAX_READ_BYTES, authorize_read_path
from app.tools.runner import execute_tool


def test_read_files_uses_injected_workspace_and_normalizes_relative_path(tmp_path: Path) -> None:
    """工作空间由执行层注入，模型只得到相对路径与文件正文。"""
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "guide.md").write_bytes("用户授权资料\n第二行".encode())
    result = execute_tool("read_files", {"path": "notes\\guide.md"}, workspace=tmp_path)
    assert result == {
        "ok": True, "path": "notes/guide.md", "content": "用户授权资料\n第二行",
    }
    assert str(tmp_path) not in str(result)


def test_read_files_requires_explicit_workspace() -> None:
    """没有执行层工作空间时拒绝，不能回退到代码目录或进程工作目录。"""
    with pytest.raises(ProviderFailure, match="^workspace_not_configured$"):
        execute_tool("read_files", {"path": "guide.md"})


@pytest.mark.parametrize("extra", [{"workspace": "/"}, {"user_id": "another-user"}])
def test_model_cannot_override_workspace(tmp_path: Path, extra: dict[str, str]) -> None:
    """模型提供的授权目录或用户身份字段不能进入调用。"""
    with pytest.raises(ValidationError):
        execute_tool("read_files", {"path": "guide.md", **extra}, workspace=tmp_path)


@pytest.mark.parametrize("relative", [
    "../outside.md", "/private.md", "C:\\private.md", "C:private.md",
    "\\\\host\\share\\private.md", "guide.md:secret", "guide.md\x00",
])
def test_read_files_rejects_escape_absolute_and_device_paths(tmp_path: Path, relative: str) -> None:
    """跨平台拒绝越界、绝对路径、网络共享、备用数据流及 NUL 路径。"""
    root = tmp_path / "workspace"
    root.mkdir()
    (tmp_path / "outside.md").write_text("不应读取", encoding="utf-8")
    with pytest.raises(ProviderFailure, match="^file_path_denied$"):
        execute_tool("read_files", {"path": relative}, workspace=root)


def test_read_files_rejects_symlink_to_external_file(tmp_path: Path) -> None:
    """解析后的软链接目标仍必须处于授权工作空间内。"""
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("不应读取", encoding="utf-8")
    try:
        (root / "linked.md").symlink_to(outside)
    except OSError:
        pytest.skip("当前主机未允许创建测试软链接")
    with pytest.raises(ProviderFailure, match="^file_path_denied$"):
        execute_tool("read_files", {"path": "linked.md"}, workspace=root)


@pytest.mark.parametrize("kind", ["directory", "unsupported", "missing"])
def test_read_files_rejects_non_text_targets(tmp_path: Path, kind: str) -> None:
    """目录、未允许扩展名和缺失文件不成为可读正文。"""
    path = (
        "folder.md" if kind == "directory" else "data.json" if kind == "unsupported" else "gone.md"
    )
    if kind == "directory":
        (tmp_path / path).mkdir()
    elif kind == "unsupported":
        (tmp_path / path).write_text("{}", encoding="utf-8")
    code = "file_unavailable" if kind == "missing" else "file_path_denied"
    with pytest.raises(ProviderFailure, match=f"^{code}$"):
        execute_tool("read_files", {"path": path}, workspace=tmp_path)


@pytest.mark.parametrize("size", [0, MAX_READ_BYTES, MAX_READ_BYTES + 1])
def test_read_files_enforces_byte_limit_without_truncation(tmp_path: Path, size: int) -> None:
    """空文本合法，恰好上限完整返回，超出上限明确失败。"""
    (tmp_path / "guide.txt").write_bytes(b"x" * size)
    if size > MAX_READ_BYTES:
        with pytest.raises(ProviderFailure, match="^file_too_large$"):
            execute_tool("read_files", {"path": "guide.txt"}, workspace=tmp_path)
    else:
        result = execute_tool("read_files", {"path": "guide.txt"}, workspace=tmp_path)
        assert result["content"] == "x" * size


def test_read_files_rejects_invalid_utf8(tmp_path: Path) -> None:
    """编码错误使用固定业务码，不返回解码异常或绝对路径。"""
    (tmp_path / "guide.txt").write_bytes(b"\xff\xfe")
    with pytest.raises(ProviderFailure, match="^file_encoding_invalid$"):
        execute_tool("read_files", {"path": "guide.txt"}, workspace=tmp_path)


def test_cached_path_recheck_rejects_removed_source(tmp_path: Path) -> None:
    """执行层可在账本复用前调用同一检查，删除后不再通过来源检查。"""
    target = tmp_path / "guide.md"
    target.write_text("旧正文", encoding="utf-8")
    assert authorize_read_path(tmp_path, "guide.md") == target
    target.unlink()
    with pytest.raises(ProviderFailure, match="^file_unavailable$"):
        authorize_read_path(tmp_path, "guide.md")


def word_document(text: str = "前言", table: bool = True) -> bytes:
    """生成包含正文和可选表格的隔离 Word 文件。"""
    document = Document()
    document.add_paragraph(text)
    if table:
        cells = document.add_table(rows=1, cols=2).rows[0].cells
        cells[0].text, cells[1].text = "负责人", "张三"
        document.add_paragraph("结尾")
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def test_word_preserves_paragraph_and_table_order(tmp_path: Path) -> None:
    """Word 二进制按正文顺序提取文字和表格，不当作 UTF-8 文件解码。"""
    (tmp_path / "资料.docx").write_bytes(word_document())
    result = execute_tool("read_files", {"path": "资料.docx"}, workspace=tmp_path)
    assert result == {
        "ok": True, "path": "资料.docx", "content": "前言\n负责人\t张三\n结尾",
    }


def test_old_word_format_has_explicit_conversion_error(tmp_path: Path) -> None:
    """旧 DOC 有清晰转换提示，缓存来源复核仍校验其路径。"""
    target = tmp_path / "资料.doc"
    target.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1"))
    assert authorize_read_path(tmp_path, "资料.doc") == target
    with pytest.raises(ProviderFailure, match="^file_format_unsupported$"):
        execute_tool("read_files", {"path": "资料.doc"}, workspace=tmp_path)


@pytest.mark.parametrize("content", [b"not a word document", b"PK\x03\x04", b"\xd0\xcf\x11\xe0"])
def test_invalid_or_encrypted_word_never_becomes_text(tmp_path: Path, content: bytes) -> None:
    """损坏 ZIP、伪造扩展名与加密 Office 容器使用固定文档错误码。"""
    (tmp_path / "资料.docx").write_bytes(content)
    with pytest.raises(ProviderFailure, match="^file_document_invalid$"):
        execute_tool("read_files", {"path": "资料.docx"}, workspace=tmp_path)


def test_word_input_byte_limit_precedes_parsing(tmp_path: Path) -> None:
    """大 Word 输入在读取边界被拒绝，不先交给解析器。"""
    with (tmp_path / "资料.docx").open("wb") as stream:
        stream.truncate(files.MAX_WORD_BYTES + 1)
    with pytest.raises(ProviderFailure, match="^file_too_large$"):
        execute_tool("read_files", {"path": "资料.docx"}, workspace=tmp_path)


def test_word_text_output_limit_counts_utf8_bytes(tmp_path: Path) -> None:
    """很小的压缩包仍可能产生超量中文，提取输出按 UTF-8 字节限制。"""
    (tmp_path / "资料.docx").write_bytes(word_document("字" * (MAX_READ_BYTES // 3 + 1), False))
    with pytest.raises(ProviderFailure, match="^file_too_large$"):
        execute_tool("read_files", {"path": "资料.docx"}, workspace=tmp_path)


@pytest.mark.parametrize(
    "limit", ["MAX_WORD_MEMBERS", "MAX_WORD_MEMBER_BYTES", "MAX_WORD_ARCHIVE_BYTES"]
)
def test_word_zip_limits_precede_document_parsing(tmp_path: Path, monkeypatch, limit: str) -> None:
    """压缩包成员数、单项展开量和总展开量都在文档解析前限制。"""
    monkeypatch.setattr(files, limit, 1)
    (tmp_path / "资料.docx").write_bytes(word_document())
    with pytest.raises(ProviderFailure, match="^file_too_large$"):
        execute_tool("read_files", {"path": "资料.docx"}, workspace=tmp_path)


def test_encrypted_zip_flag_has_stable_document_error(tmp_path: Path) -> None:
    """加密 ZIP 不要求密码、不回传原始解析错误。"""
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "<document/>")
    content = bytearray(output.getvalue())
    offset = content.index(b"PK\x01\x02")
    content[offset + 8:offset + 10] = (1).to_bytes(2, "little")
    (tmp_path / "资料.docx").write_bytes(content)
    with pytest.raises(ProviderFailure, match="^file_document_invalid$"):
        execute_tool("read_files", {"path": "资料.docx"}, workspace=tmp_path)


def test_word_external_entity_cannot_read_other_files(tmp_path: Path) -> None:
    """Word XML 中的外部实体不会把工作空间外的正文带入结果。"""
    root = tmp_path / "workspace"
    root.mkdir()
    secret = tmp_path / "outside.txt"
    secret.write_text("private-external-content", encoding="utf-8")
    output = io.BytesIO()
    with ZipFile(io.BytesIO(word_document()), "r") as source, ZipFile(output, "w") as target:
        for member in source.infolist():
            content = source.read(member.filename)
            if member.filename == "word/document.xml":
                entity = (
                    f'<!DOCTYPE w:document [<!ENTITY outside SYSTEM "{secret.as_uri()}">]>'
                ).encode()
                content = content.replace(b"<w:document ", entity + b"<w:document ", 1)
                content = content.replace("前言".encode(), b"&outside;")
            target.writestr(member.filename, content)
    (root / "资料.docx").write_bytes(output.getvalue())
    try:
        result = execute_tool("read_files", {"path": "资料.docx"}, workspace=root)
    except ProviderFailure as error:
        assert error.code == "file_document_invalid"
    else:
        assert "private-external-content" not in str(result)


def test_word_rejects_unbounded_table_grid_expansion(tmp_path: Path) -> None:
    """少量 XML 不能借网格跨度声明展开成海量重复单元格。"""
    output = io.BytesIO()
    with ZipFile(io.BytesIO(word_document()), "r") as source, ZipFile(output, "w") as target:
        for member in source.infolist():
            content = source.read(member.filename)
            if member.filename == "word/document.xml":
                assert b"</w:tcPr>" in content
                content = content.replace(
                    b"</w:tcPr>", b'<w:gridSpan w:val="1000000000"/></w:tcPr>', 1,
                )
            target.writestr(member.filename, content)
    (tmp_path / "资料.docx").write_bytes(output.getvalue())
    with pytest.raises(ProviderFailure, match="^file_document_invalid$"):
        execute_tool("read_files", {"path": "资料.docx"}, workspace=tmp_path)


def test_word_limits_total_expanded_cells_before_materializing_rows(tmp_path: Path) -> None:
    """多个合法跨度也不能累加出无界单元格列表。"""
    document = Document()
    table = document.add_table(rows=1, cols=40)
    for cell in table.rows[0].cells:
        span = OxmlElement("w:gridSpan")
        span.set("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val", "256")
        cell._tc.get_or_add_tcPr().append(span)
    document.save(tmp_path / "资料.docx")
    with pytest.raises(ProviderFailure, match="^file_too_large$"):
        execute_tool("read_files", {"path": "资料.docx"}, workspace=tmp_path)

"""仅读取执行层注入的用户工作空间，不接收模型提供的授权根目录。"""

import io
from pathlib import Path, PureWindowsPath
from zipfile import ZipFile

from docx import Document
from docx.table import Table

from app.providers.client import ProviderFailure
from app.tools.builtin.contracts import ReadFiles

MAX_READ_BYTES = 64 * 1024
MAX_WORD_BYTES = 10 * 1024 * 1024
MAX_WORD_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_WORD_MEMBER_BYTES = 8 * 1024 * 1024
MAX_WORD_MEMBERS = 1000
MAX_WORD_CELLS = 10000


def authorize_read_path(workspace: Path, relative: str) -> Path:
    """复核相对路径、目录归属及类型，供执行与成功账本复用前调用。"""
    path = Path(relative.replace("\\", "/"))
    if (
        path.anchor
        or PureWindowsPath(relative).drive
        or ":" in relative
        or "\x00" in relative
    ):
        raise ProviderFailure("file_path_denied")
    try:
        root = workspace.resolve(strict=True)
        if not root.is_dir():
            raise ProviderFailure("workspace_unavailable")
        target = (root / path).resolve(strict=True)
        if (
            not target.is_relative_to(root)
            or not target.is_file()
            or target.suffix.lower() not in {".txt", ".md", ".docx", ".doc"}
        ):
            raise ProviderFailure("file_path_denied")
    except (OSError, RuntimeError):
        raise ProviderFailure("file_unavailable") from None
    return target


def word_text(content: bytes) -> str:
    """本地解析 Word 正文与表格，先限制 ZIP 展开量，再限制提取文本量。"""
    try:
        with ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            if (
                len(members) > MAX_WORD_MEMBERS
                or sum(member.file_size for member in members) > MAX_WORD_ARCHIVE_BYTES
                or any(member.file_size > MAX_WORD_MEMBER_BYTES for member in members)
            ):
                raise ProviderFailure("file_too_large")
            if any(member.flag_bits & 1 for member in members):
                raise ProviderFailure("file_document_invalid")
        document = Document(io.BytesIO(content))
        # 防止恶意网格跨度把少量 XML 单元格扩展为无界 Python 单元格列表。
        expanded_cells = len(document.element.xpath(".//w:tc"))
        for span in document.element.xpath(".//w:gridSpan"):
            value = span.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val")
            count = int(value or "1")
            if not 1 <= count <= 256:
                raise ProviderFailure("file_document_invalid")
            expanded_cells += count - 1
        if expanded_cells > MAX_WORD_CELLS:
            raise ProviderFailure("file_too_large")
        parts: list[str] = []
        size = 0
        for block in document.iter_inner_content():
            lines = (
                ("\t".join(cell.text for cell in row.cells) for row in block.rows)
                if isinstance(block, Table) else (block.text,)
            )
            for line in lines:
                size += len(line.encode("utf-8")) + (1 if parts else 0)
                if size > MAX_READ_BYTES:
                    raise ProviderFailure("file_too_large")
                parts.append(line)
        return "\n".join(parts)
    except ProviderFailure:
        raise
    except Exception:
        # 非法 ZIP、缺失 Office 部件、加密容器及 XML 解析错误使用同一公开错误码。
        raise ProviderFailure("file_document_invalid") from None


def read_files(arguments: object, workspace: Path) -> dict[str, object]:
    """限量读取文本或提取 Word 正文，失败时只携带固定错误码。"""
    args = ReadFiles.model_validate(arguments)
    target = authorize_read_path(workspace, args.path)
    suffix = target.suffix.lower()
    if suffix == ".doc":
        raise ProviderFailure("file_format_unsupported")
    limit = MAX_WORD_BYTES if suffix == ".docx" else MAX_READ_BYTES
    try:
        with target.open("rb") as stream:
            content = stream.read(limit + 1)
    except OSError:
        raise ProviderFailure("file_unavailable") from None
    if len(content) > limit:
        raise ProviderFailure("file_too_large")
    text = content_text(content, suffix)
    return {
        "ok": True,
        "path": target.relative_to(workspace.resolve()).as_posix(),
        "content": text,
    }


def content_text(content: bytes, suffix: str) -> str:
    """目录读取和导入快照共用同一格式、大小与文本提取边界。"""
    if suffix not in {".txt", ".md", ".docx"}:
        raise ProviderFailure("file_format_unsupported")
    if len(content) > (MAX_WORD_BYTES if suffix == ".docx" else MAX_READ_BYTES):
        raise ProviderFailure("file_too_large")
    if suffix == ".docx":
        return word_text(content)
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        raise ProviderFailure("file_encoding_invalid") from None

"""对话附件解析；仅处理用户主动上传的字节，不访问本机路径。"""

import io
from pathlib import PurePosixPath
from urllib.parse import unquote
from zipfile import ZipFile

from docx import Document
from fastapi import APIRouter, HTTPException, Request
from pypdf import PdfReader
from starlette.concurrency import run_in_threadpool

from app.identity.dependencies import Writer

router = APIRouter(prefix="/api/v1/chat", tags=["对话附件"])
MAX_BYTES = 20 * 1024 * 1024
MAX_TEXT = 60000


def extract(content: bytes, filename: str) -> tuple[str, bool]:
    """提取正文并明确标记截取，不执行 Office 宏或 PDF 脚本。"""
    suffix = PurePosixPath(filename).suffix.lower()
    parts: list[str] = []
    size = 0
    truncated = False

    def append(value: str) -> bool:
        """在累计文本上限处停止，避免大文档生成无限上下文。"""
        nonlocal size, truncated
        raw = (value + "\n").encode("utf-8")
        remaining = MAX_TEXT - size
        parts.append(raw[:remaining].decode("utf-8", errors="ignore"))
        size += min(len(raw), remaining)
        truncated = len(raw) > remaining
        return not truncated

    if suffix == ".docx":
        with ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            if (len(members) > 1000 or sum(m.file_size for m in members) > 32 * 1024**2
                    or any(m.file_size > 8 * 1024**2 or m.flag_bits & 1 for m in members)):
                raise ValueError("invalid Word archive")
        doc = Document(io.BytesIO(content))
        # 直接取 XML 文本，不展开可被恶意 gridSpan 放大的表格单元格。
        for paragraph in doc.element.body.xpath(".//w:p"):
            if not append("".join(paragraph.xpath(".//w:t/text()"))):
                break
    elif suffix == ".pdf":
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted or len(reader.pages) > 200:
            raise ValueError("encrypted or oversized PDF")
        for page in reader.pages:
            if not append(page.extract_text() or ""):
                break
    elif suffix in {".txt", ".md", ".csv", ".json", ".log", ".rst"}:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("gb18030")
        append(text)
    else:
        raise ValueError("unsupported format")
    text = "".join(parts).strip()
    if not text:
        raise ValueError("empty or scanned document")
    return text, truncated


@router.post("/uploads")
async def upload(request: Request, actor: Writer) -> dict[str, object]:
    """认证及 CSRF 校验后有界接收单文件，返回仅供当前消息使用的文本。"""
    filename = unquote(request.headers.get("X-Filename", ""))
    if (not filename or len(filename) > 255 or any(ord(c) < 32 for c in filename)
            or "/" in filename or "\\" in filename):
        raise HTTPException(422)
    content = bytearray()
    async for block in request.stream():
        if len(content) + len(block) > MAX_BYTES:
            raise HTTPException(413)
        content.extend(block)
    try:
        text, truncated = await run_in_threadpool(extract, bytes(content), filename)
    except Exception:
        raise HTTPException(422) from None
    return {"filename": filename, "size": len(content), "text": text, "truncated": truncated}

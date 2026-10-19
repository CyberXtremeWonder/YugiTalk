"""独立子进程解析文件：父进程限制时间，Linux 同时限制内存和 CPU。"""

import io
import json
import os
import re
import sys
from typing import Any

from pypdf import PdfReader
from pypdf.errors import PdfReadError


def parse(content: bytes, suffix: str, strategy: str = "fixed") -> list[dict[str, object]]:
    """解析受支持的文本或文本文档，返回带页码和顺序的重叠分块。"""
    if not content or len(content) > 20 * 1024 * 1024:
        raise ValueError("empty_or_oversized_file")
    if strategy not in {"fixed", "markdown_sections"} or (
        strategy == "markdown_sections" and suffix != ".md"
    ):
        raise ValueError("document_chunk_strategy_invalid")
    pages: list[tuple[int | None, str]] = []
    if suffix == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise ValueError("invalid_pdf")
        reader = PdfReader(io.BytesIO(content), strict=True)
        if reader.is_encrypted or len(reader.pages) > 200:
            raise ValueError("pdf_encrypted_or_too_many_pages")
        total = 0
        for number, page in enumerate(reader.pages, 1):
            stream = page.get_contents()
            if stream and len(stream.get_data()) > 8 * 1024 * 1024:
                raise ValueError("pdf_page_too_large")
            text = page.extract_text() or ""
            total += len(text)
            if total > 500000:
                raise ValueError("text_too_large")
            # PDF 字体映射可能生成 NUL；它没有可显示内容且 PostgreSQL 文本列禁止存储。
            text = text.replace("\x00", "")
            pages.append((number, text))
        if not any(text.strip() for _, text in pages):
            raise ValueError("pdf_requires_ocr")
    elif suffix in {".txt", ".md"}:
        text = content.decode("utf-8-sig", errors="strict")
        if "\x00" in text or len(text) > 500000 or content.startswith(b"%PDF-"):
            raise ValueError("invalid_text")
        pages = [(None, text)]
    else:
        raise ValueError("unsupported_file_type")
    chunks: list[dict[str, object]] = []
    for page_number, text in pages:
        text = text.strip()
        boundaries = [0]
        if strategy == "markdown_sections":
            boundaries += [
                match.start()
                for match in re.finditer(r"(?m)^#{1,6}[ \t]+[^\n]*$", text)
                if match.start() > 0
            ]
        boundaries.append(len(text))
        for section, (begin, end) in enumerate(zip(boundaries, boundaries[1:], strict=False)):
            section_text = text[begin:end].strip()
            stop = len(section_text) if strategy == "fixed" else max(1, len(section_text) - 100)
            for start in range(0, stop, 600):
                part = section_text[start : start + 700].strip()
                if part:
                    chunk: dict[str, object] = {
                        "page": page_number,
                        "content": part,
                        "ordinal": len(chunks) + 1,
                    }
                    if strategy == "markdown_sections":
                        chunk["section"] = section
                    chunks.append(chunk)
    if not chunks:
        raise ValueError("empty_document")
    if len(chunks) > 1000:
        raise ValueError("too_many_chunks")
    return chunks


def main() -> None:
    """作为受限解析子进程入口，读取文件并输出结构化分块结果。"""
    if os.name != "nt":
        import importlib

        resource: Any = importlib.import_module("resource")

        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    result: dict[str, object]
    try:
        result = {
            "chunks": parse(
                sys.stdin.buffer.read(20 * 1024 * 1024 + 1),
                sys.argv[1],
                sys.argv[2] if len(sys.argv) > 2 else "fixed",
            )
        }
    except Exception as error:
        allowed = {
            "empty_or_oversized_file",
            "invalid_pdf",
            "pdf_encrypted_or_too_many_pages",
            "pdf_page_too_large",
            "text_too_large",
            "pdf_requires_ocr",
            "invalid_text",
            "unsupported_file_type",
            "empty_document",
            "too_many_chunks",
            "document_chunk_strategy_invalid",
        }
        code = (
            "invalid_pdf"
            if isinstance(error, PdfReadError)
            else str(error)
            if isinstance(error, ValueError) and str(error) in allowed
            else "document_parse_failed"
        )
        result = {"error": code}
    sys.stdout.write(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()

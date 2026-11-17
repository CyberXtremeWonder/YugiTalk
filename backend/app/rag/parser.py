"""独立子进程解析文件：父进程限制时间，Linux 同时限制内存和 CPU。"""

import io
import json
import os
import re
import sys
from typing import Any

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError

from app.rag.chunking import PROFILES, chunk_pages
from app.rag.docling import DOCLING_SUFFIXES, document_pages
from app.rag.limits import MAX_DOCUMENT_CHUNKS, MAX_DOCUMENT_PAGES, MAX_TEXT_CHARACTERS


def parse(
    content: bytes, suffix: str, strategy: str = "fixed", *, optimize_jsonl: bool = True,
) -> list[dict[str, object]]:
    """解析受支持的文本或文本文档，返回带页码和顺序的重叠分块。"""
    if not content or len(content) > 20 * 1024 * 1024:
        raise ValueError("empty_or_oversized_file")
    if strategy not in {"fixed", "markdown_sections", "auto", *PROFILES} or (
        strategy == "markdown_sections" and suffix != ".md"
    ):
        raise ValueError("document_chunk_strategy_invalid")
    pages: list[tuple[int | None, str]] = []
    if suffix == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise ValueError("invalid_pdf")
        reader = PdfReader(io.BytesIO(content), strict=True)
        encrypted = reader.is_encrypted
        if encrypted and not reader.decrypt(""):
            raise ValueError("pdf_password_required")
        if len(reader.pages) > MAX_DOCUMENT_PAGES:
            raise ValueError("pdf_too_many_pages")
        for page in reader.pages:
            stream = page.get_contents()
            if stream and len(stream.get_data()) > 8 * 1024 * 1024:
                raise ValueError("pdf_page_too_large")
        if encrypted:
            # 空密码可打开的 PDF 仍可能带加密标记；仅在内存中转换，原始文件保持原样。
            writer = PdfWriter()
            writer.clone_document_from_reader(reader)
            decrypted = io.BytesIO()
            writer.write(decrypted)
            content = decrypted.getvalue()
        pages = document_pages(content, suffix)
    elif suffix in {".txt", ".md", ".jsonl"}:
        text = content.decode("utf-8-sig", errors="strict")
        if len(text) > MAX_TEXT_CHARACTERS:
            raise ValueError("text_too_large")
        if "\x00" in text or content.startswith(b"%PDF-"):
            raise ValueError("invalid_text")
        if suffix == ".jsonl":
            for line in text.split("\n"):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    rendered = json.dumps(record, ensure_ascii=False, allow_nan=False)
                    if optimize_jsonl and isinstance(record, dict) and isinstance(
                        record.get("text"), str
                    ):
                        title = record.get("title")
                        if title is None or isinstance(title, str):
                            extra = {k: v for k, v in record.items() if k not in {"title", "text"}}
                            rendered = "\n\n".join(value for value in (
                                title, record["text"],
                                json.dumps(extra, ensure_ascii=False) if extra else "",
                            ) if value)
                except ValueError:
                    raise ValueError("invalid_jsonl") from None
                pages.append((None, rendered))
                if len(pages) > MAX_DOCUMENT_CHUNKS:
                    raise ValueError("too_many_chunks")
            if sum(len(record) for _, record in pages) > MAX_TEXT_CHARACTERS:
                raise ValueError("text_too_large")
        else:
            pages = [(None, text)]
    elif suffix in DOCLING_SUFFIXES:
        pages = document_pages(content, suffix)
    else:
        raise ValueError("unsupported_file_type")
    if strategy not in {"fixed", "markdown_sections"}:
        return chunk_pages(pages, strategy, merge_short_sentences=(
            suffix == ".jsonl" and optimize_jsonl
        ))
    chunks: list[dict[str, object]] = []
    for record_number, (page_number, text) in enumerate(pages):
        text = text.strip()
        boundaries = [0]
        if strategy == "markdown_sections":
            boundaries += [
                match.start()
                for match in re.finditer(r"(?m)^#{1,6}[ \t]+[^\n]*$", text)
                if match.start() > 0
            ]
        boundaries.append(len(text))
        # Markdown 先按标题划分章节，再在章节内切块；普通文本/每页 PDF 则视作一段。
        for section, (begin, end) in enumerate(zip(boundaries, boundaries[1:], strict=False)):
            section_text = text[begin:end].strip()
            stop = len(section_text) if strategy == "fixed" else max(1, len(section_text) - 100)
            for start in range(0, stop, 600):
                # 每块 700 字、步长 600，保留最多 100 字交叠，减少边界切断语义的影响。
                part = section_text[start : start + 700].strip()
                if part:
                    chunk: dict[str, object] = {
                        "page": page_number,
                        "content": part,
                        "ordinal": len(chunks) + 1,
                    }
                    if strategy == "markdown_sections":
                        chunk["section"] = section
                    if suffix == ".jsonl":
                        chunk["section"] = record_number
                    chunks.append(chunk)
    if not chunks:
        raise ValueError("empty_document")
    if len(chunks) > MAX_DOCUMENT_CHUNKS:
        raise ValueError("too_many_chunks")
    return chunks


def main() -> None:
    """作为受限解析子进程入口，读取文件并输出结构化分块结果。"""
    if os.name != "nt":
        import importlib

        resource: Any = importlib.import_module("resource")

        resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
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
            "pdf_password_required",
            "pdf_too_many_pages",
            "pdf_page_too_large",
            "text_too_large",
            "invalid_text",
            "invalid_jsonl",
            "unsupported_file_type",
            "empty_document",
            "too_many_chunks",
            "document_chunk_strategy_invalid",
            "chunk_tokenizer_unavailable",
            "chunk_tokenizer_model_mismatch",
            "invalid_office",
            "office_expanded_too_large",
            "docling_unavailable",
            "docling_configuration",
            "docling_conversion_failed",
            "docling_response_invalid",
            "docling_response_too_large",
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

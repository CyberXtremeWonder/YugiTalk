"""通过本地 Docling 服务统一解析 PDF、Office 和图片，保留表格与页边界。"""

import io
import json
from typing import Any
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile

import httpx

from app.core.config import Settings
from app.rag.limits import (
    MAX_DOCLING_RESPONSE_BYTES,
    MAX_DOCUMENT_PAGES,
    MAX_TEXT_CHARACTERS,
)

DOCLING_SUFFIXES = {
    ".pdf", ".xlsx", ".xls", ".docx", ".doc",
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp",
}
PAGE_BREAK = "<!-- YUGITALK_PAGE_BREAK -->"


def document_pages(content: bytes, suffix: str) -> list[tuple[int | None, str]]:
    """请求完整转换结果；拒绝残缺转换及超大响应，不回退到旧解析器。"""
    if suffix in {".xlsx", ".docx"}:
        try:
            with ZipFile(io.BytesIO(content)) as archive:
                if sum(item.file_size for item in archive.infolist()) > 32 * 1024 * 1024:
                    raise ValueError("office_expanded_too_large")
        except BadZipFile:
            raise ValueError("invalid_office") from None
    endpoint = Settings().docling_url.rstrip("/")
    address = urlsplit(endpoint)
    if address.scheme not in {"http", "https"} or not address.hostname or (
        address.username or address.password or address.query or address.fragment
    ):
        raise ValueError("docling_configuration")
    try:
        with httpx.Client(timeout=620, trust_env=False, follow_redirects=False) as client:
            with client.stream(
                "POST", f"{endpoint}/v1/convert/file",
                files={"files": (f"document{suffix}", content, "application/octet-stream")},
                data={
                    "to_formats": ["md", "json"], "do_ocr": "true",
                    "do_table_structure": "true", "table_mode": "accurate",
                    "ocr_engine": "rapidocr", "image_export_mode": "placeholder",
                    "include_images": "false", "do_picture_description": "false",
                    "md_page_break_placeholder": PAGE_BREAK,
                    "document_timeout": "600", "abort_on_error": "true",
                },
            ) as response:
                if response.status_code >= 500 or response.status_code == 429:
                    raise ValueError("docling_unavailable")
                if response.status_code != 200:
                    raise ValueError("docling_conversion_failed")
                raw = bytearray()
                for block in response.iter_bytes():
                    raw.extend(block)
                    if len(raw) > MAX_DOCLING_RESPONSE_BYTES:
                        raise ValueError("docling_response_too_large")
    except httpx.TimeoutException:
        raise ValueError("document_parse_timeout") from None
    except httpx.HTTPError:
        raise ValueError("docling_unavailable") from None
    try:
        payload = json.loads(raw)
        if payload["status"] != "success":
            raise ValueError("docling_conversion_failed")
        document = payload["document"]
        text = document["md_content"]
        source = document.get("json_content", {})
        source_pages = source.get("pages", {})
        if not isinstance(text, str) or not isinstance(source_pages, dict):
            raise ValueError("docling_response_invalid")
        text = text.replace("\x00", "").replace("<!-- image -->", "")
        numbers = sorted(int(number) for number in source_pages)
        if any(number < 1 for number in numbers):
            raise ValueError("docling_response_invalid")
        if len(numbers) > MAX_DOCUMENT_PAGES:
            raise ValueError("pdf_too_many_pages")
        if len(text) > MAX_TEXT_CHARACTERS:
            raise ValueError("text_too_large")
        if suffix in {".xlsx", ".xls"} and any(
            group.get("label") == "sheet" for group in source.get("groups", [])
        ):
            return spreadsheet_pages(source)
        parts = text.split(PAGE_BREAK)
        # 只有页边界与 Docling 的实际页号一一对应时才提供页码，避免伪造引用位置。
        if len(numbers) == len(parts):
            return list(zip(numbers, parts, strict=True))
        return [(None, text.replace(PAGE_BREAK, "\n\n"))]
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError):
        raise ValueError("docling_response_invalid") from None
    except ValueError as error:
        if str(error) in {
            "docling_conversion_failed", "docling_response_invalid",
            "pdf_too_many_pages", "text_too_large",
        }:
            raise
        raise ValueError("docling_response_invalid") from None


def spreadsheet_pages(source: dict[str, Any]) -> list[tuple[int | None, str]]:
    """从 Docling 的结构化表格保留工作表名、列名和行号，避免跨表格串义。"""
    tables = {table["self_ref"]: table for table in source.get("tables", [])}
    pages = []
    length = 0
    for group in source.get("groups", []):
        if group.get("label") != "sheet":
            continue
        for child in group.get("children", []):
            table = tables.get(child["$ref"])
            if table is None:
                continue
            grid = table["data"]["grid"]
            if not grid:
                continue
            name = group["name"]
            headers = [str(cell["text"]).strip() for cell in grid[0]]
            lines = [f"工作表：{name}；表头：{'；'.join(headers)}"]
            for number, row in enumerate(grid[1:], 2):
                cells = []
                for index, cell in enumerate(row):
                    value = str(cell["text"]).strip()
                    if value:
                        label = headers[index] if index < len(headers) else f"第{index + 1}列"
                        cells.append(f"{label}={value}")
                if cells:
                    lines.append(f"工作表：{name}；表内第{number}行：{'；'.join(cells)}")
            text = "\n".join(lines).replace("\x00", "")
            length += len(text)
            if length > MAX_TEXT_CHARACTERS:
                raise ValueError("text_too_large")
            prov = table.get("prov", [])
            page = prov[0]["page_no"] if prov else None
            pages.append((page, text))
    return pages

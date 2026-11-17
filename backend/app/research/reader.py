"""读取公共 HTTPS 原文；复用 DNS 固定，不携带凭证且不跟随重定向。"""

import hashlib
import io
import socket
import threading
import time
from collections.abc import Callable
from html.parser import HTMLParser
from urllib.parse import urlsplit

from pypdf import PdfReader

from app.providers.client import ProviderFailure
from app.tools.http.runtime import PinnedHTTPSConnection, resolve_public
from app.tools.http.search import source_url


class PageText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hidden = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def read_source(url: str, guard: Callable[[], None]) -> dict[str, object]:
    """保存有界快照和截断标记；搜索摘要不会伪装成已读全文。"""
    target = urlsplit(url)
    if not source_url(url) or target.scheme != "https":
        return {"read_status": "https_required", "content": ""}
    deadline = time.monotonic() + 20
    guard()
    address = resolve_public(target.hostname or "", deadline)
    guard()
    connection = PinnedHTTPSConnection(
        target.hostname or "", address, max(0.01, deadline - time.monotonic())
    )

    def abort() -> None:
        if connection.sock:
            try:
                connection.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        connection.close()

    timer = threading.Timer(max(0.01, deadline - time.monotonic()), abort)
    timer.daemon = True
    timer.start()
    try:
        path = (target.path or "/") + ("?" + target.query if target.query else "")
        connection.request(
            "GET",
            path,
            headers={"Accept-Encoding": "identity", "User-Agent": "YugiTalk-Research/1.0"},
        )
        response = connection.getresponse()
        if response.status != 200:
            return {"read_status": f"http_{response.status}", "content": ""}
        kind = response.headers.get_content_type()
        if kind not in {"text/html", "text/plain", "application/pdf"}:
            return {"read_status": "unsupported_type", "content": ""}
        body = bytearray()
        while True:
            guard()
            if time.monotonic() >= deadline:
                raise TimeoutError
            piece = response.read(32768)
            if not piece:
                break
            body.extend(piece)
            if len(body) > 4 * 1024 * 1024:
                return {"read_status": "too_large", "content": ""}
        text = ""
        truncated = False
        if kind == "application/pdf":
            reader = PdfReader(io.BytesIO(body))
            for number, page in enumerate(reader.pages):
                guard()
                if number >= 40 or len(text) >= 24000 or time.monotonic() >= deadline:
                    truncated = True
                    break
                text += f"\n[page {number + 1}]\n" + (page.extract_text() or "")
        else:
            text = body.decode(response.headers.get_content_charset() or "utf-8", errors="replace")
            if kind == "text/html":
                parser = PageText()
                parser.feed(text)
                text = "\n".join(parser.parts)
        return {
            "read_status": "read" if text.strip() else "empty",
            "content": text[:24000],
            "truncated": truncated or len(text) > 24000,
            "content_hash": hashlib.sha256(body).hexdigest(),
            "media_type": kind,
        }
    except ProviderFailure:
        raise
    except Exception:
        return {"read_status": "read_failed", "content": ""}
    finally:
        timer.cancel()
        connection.close()

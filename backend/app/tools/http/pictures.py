"""有界图片抓取：公网 HTTPS、固定 DNS、逐跳校验及真实图片验证。"""

import http.client
import io
import socket
import threading
import time
import warnings
from collections.abc import Callable
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from PIL import Image, UnidentifiedImageError

from app.providers.client import ProviderFailure
from app.tools.http.pictures_contract import FetchPicture
from app.tools.http.runtime import PinnedHTTPSConnection, resolve_public

LIMIT = 5 * 1024 * 1024
FORMATS = {
    "PNG": ("png", "image/png"),
    "JPEG": ("jpg", "image/jpeg"),
    "WEBP": ("webp", "image/webp"),
    "GIF": ("gif", "image/gif"),
}


def read_url(
    url: str, guard: Callable[[], None], deadline: float, *, accept: str = "image/*,text/html",
) -> tuple[bytes, str, str]:
    """不发送凭据，不读取代理；所有重定向重新校验公网地址。"""
    for _ in range(4):
        guard()
        try:
            parsed = urlsplit(url)
            valid = (
                len(url) <= 2000
                and parsed.scheme == "https"
                and parsed.hostname
                and parsed.port in {None, 443}
                and not parsed.username
                and not parsed.password
                and not any(ord(c) <= 32 for c in url)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ProviderFailure("tool_network_denied")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderFailure("tool_timeout")
        address = resolve_public(str(parsed.hostname), deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderFailure("tool_timeout")
        connection = PinnedHTTPSConnection(str(parsed.hostname), address, remaining)

        def abort(target: PinnedHTTPSConnection = connection) -> None:
            if target.sock:
                try:
                    target.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            target.close()

        watchdog = threading.Timer(remaining, abort)
        watchdog.daemon = True
        watchdog.start()
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            connection.request(
                "GET",
                path,
                headers={
                    "Accept": accept,
                    "Accept-Encoding": "identity",
                    "User-Agent": "YugiTalk/1.0",
                },
            )
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ProviderFailure("tool_response_invalid")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ProviderFailure("tool_provider_rejected")
            media = (response.getheader("Content-Type") or "").split(";")[0].lower().strip()
            if response.getheader("Content-Encoding", "identity") != "identity":
                raise ProviderFailure("tool_response_invalid")
            data = bytearray()
            while True:
                guard()
                if time.monotonic() >= deadline:
                    raise ProviderFailure("tool_timeout")
                if connection.sock:
                    connection.sock.settimeout(max(0.01, deadline - time.monotonic()))
                chunk = response.read1(min(64 * 1024, LIMIT + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > LIMIT:
                    raise ProviderFailure("tool_result_too_large")
            guard()
            if time.monotonic() >= deadline:
                raise ProviderFailure("tool_timeout")
            return bytes(data), media, url
        except (OSError, http.client.HTTPException, UnicodeError):
            raise ProviderFailure("tool_network_failed") from None
        finally:
            watchdog.cancel()
            connection.close()
    raise ProviderFailure("tool_response_invalid")


class Pictures(HTMLParser):
    def __init__(self, base: str, limit: int):
        super().__init__()
        self.base, self.limit = base, limit
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "img" or len(self.urls) >= self.limit:
            return
        values = dict(attrs)
        src = values.get("data-src") or values.get("src")
        if src:
            url = urljoin(self.base, src)
            if url not in self.urls:
                self.urls.append(url)


def picture(data: bytes, index: int) -> tuple[str, str, bytes]:
    """拒绝 HTML/SVG 及伪装图片，不解码或执行外部资源。"""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in FORMATS or image.width * image.height > 25_000_000:
                    raise ValueError
                ext, media = FORMATS[image.format]
                image.verify()
    except (
        ValueError,
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise ProviderFailure("tool_response_invalid") from None
    return f"image-{index}.{ext}", media, data


def fetch_picture(arguments: object, guard: Callable[[], None]) -> list[tuple[str, str, bytes]]:
    request = FetchPicture.model_validate(arguments)
    deadline = time.monotonic() + 30
    data, media, final = read_url(request.url, guard, deadline)
    if media != "text/html":
        return [picture(data, 1)]
    parser = Pictures(final, request.max_images)
    parser.feed(data.decode("utf-8", errors="replace"))
    files: list[tuple[str, str, bytes]] = []
    for url in parser.urls:
        guard()
        try:
            content, _, _ = read_url(url, guard, deadline)
            files.append(picture(content, len(files) + 1))
        except ProviderFailure as exc:
            if not exc.code.startswith("tool_"):
                raise
            # 网页中无效或被阻止的图片不妨碍其他图片；取消由 guard 独立向上传播。
            guard()
        if sum(len(row[2]) for row in files) > 15 * 1024 * 1024:
            raise ProviderFailure("tool_result_too_large")
    if not files:
        raise ProviderFailure("tool_no_data")
    return files

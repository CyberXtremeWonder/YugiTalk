"""固定 aria2 RPC 后台下载，签名下载 ID 绑定当前用户。"""

import base64
import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from app.core.config import Settings
from app.providers.client import ProviderFailure
from app.tools.http.torrent_contract import DownloadTorrent

TOOL_NAME = "download_torrent"
STATUS_KEYS = [
    "status", "totalLength", "completedLength", "downloadSpeed", "errorCode", "followedBy", "dir",
]


def configuration_note(settings: Settings) -> str:
    if not settings.aria2_rpc_url or not settings.aria2_rpc_secret.get_secret_value():
        return "尚未配置 ARIA2_RPC_URL 和 ARIA2_RPC_SECRET"
    try:
        url = urlsplit(settings.aria2_rpc_url)
        valid = (
            url.scheme in {"http", "https"} and url.hostname and url.port != 0
            and not (url.username or url.password or url.query or url.fragment)
            and not any(ord(c) <= 32 for c in settings.aria2_rpc_url)
        )
    except ValueError:
        valid = False
    root = settings.torrent_download_root
    if not valid or not root or not (
        PurePosixPath(root).is_absolute() or PureWindowsPath(root).is_absolute()
    ):
        return "请配置有效的 aria2 RPC 地址及绝对下载目录"
    return ""


def signature(settings: Settings, owner: str, gid: str) -> str:
    return hmac.new(
        settings.aria2_rpc_secret.get_secret_value().encode(),
        f"torrent:{owner}:{gid}".encode(), hashlib.sha256,
    ).hexdigest()


def rpc(settings: Settings, method: str, params: list[object]) -> Any:
    """只连接部署者配置的服务；响应和异常均不返回凭据或原始 RPC 错误。"""
    payload = {
        "jsonrpc": "2.0", "id": "torrent", "method": f"aria2.{method}",
        "params": [f"token:{settings.aria2_rpc_secret.get_secret_value()}", *params],
    }
    try:
        with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as client:
            with client.stream("POST", settings.aria2_rpc_url, json=payload) as response:
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes(chunk_size=4096):
                    data.extend(chunk)
                    if len(data) > 64 * 1024:
                        raise ProviderFailure("tool_result_too_large")
        result = json.loads(data)
    except httpx.TimeoutException:
        raise ProviderFailure("tool_timeout") from None
    except httpx.HTTPError:
        raise ProviderFailure("tool_network_failed") from None
    except ValueError:
        raise ProviderFailure("tool_response_invalid") from None
    if not isinstance(result, dict) or result.get("id") != "torrent":
        raise ProviderFailure("tool_response_invalid")
    if "error" in result:
        raise ProviderFailure("tool_provider_rejected")
    if "result" not in result:
        raise ProviderFailure("tool_response_invalid")
    return result["result"]


def download_torrent(
    arguments: object, settings: Settings, owner: str, request_key: str,
    guard: Callable[[], None],
) -> dict[str, object]:
    try:
        request = DownloadTorrent.model_validate(arguments)
    except ValueError:
        return {"ok": False, "error_code": "tool_arguments_invalid"}
    if configuration_note(settings):
        raise ProviderFailure("tool_not_configured")
    guard()
    if request.action == "status":
        token = request.download_id or ""
        if not re.fullmatch(r"[0-9a-f]{16}\.[0-9a-f]{64}", token):
            return {"ok": False, "error_code": "tool_not_authorized"}
        gid, signed = token.split(".")
        if not hmac.compare_digest(signed, signature(settings, owner, gid)):
            return {"ok": False, "error_code": "tool_not_authorized"}
    else:
        gid = signature(settings, owner, request_key)[:16]
        token = f"{gid}.{signature(settings, owner, gid)}"
    root = settings.torrent_download_root.replace("\\", "/").rstrip("/")
    directory = f"{root}/{hashlib.sha256(owner.encode()).hexdigest()}/{gid}"
    try:
        if request.action == "start":
            source = request.source or ""
            parsed = urlsplit(source)
            options = {
                "gid": gid, "dir": directory, "seed-time": "0", "follow-torrent": "true",
                "bt-metadata-only": "false", "bt-save-metadata": "true",
                "allow-overwrite": "false", "auto-file-renaming": "false",
            }
            if parsed.scheme == "magnet":
                hashes = parse_qs(parsed.query).get("xt", [])
                if not any(re.fullmatch(
                    r"urn:btih:(?:[0-9a-fA-F]{40}|[A-Z2-7a-z]{32})", value,
                ) for value in hashes):
                    raise ValueError
                guard()
                result = rpc(settings, "addUri", [[source], options])
            elif parsed.scheme == "https":
                from app.tools.http.pictures import read_url

                content, _, _ = read_url(
                    source, guard, time.monotonic() + 30, accept="application/x-bittorrent",
                )
                guard()
                result = rpc(settings, "addTorrent", [
                    base64.b64encode(content).decode("ascii"), [], options,
                ])
            else:
                raise ValueError
            if result != gid:
                raise ProviderFailure("tool_response_invalid")
            guard()
            return {
                "ok": True, "download_id": token, "status": "submitted",
                "save_directory": directory,
                "summary": "已提交后台下载，请用 status 查询；文件保存在下载服务器。",
            }
        for _ in range(3):
            guard()
            status = rpc(settings, "tellStatus", [gid, STATUS_KEYS])
            if (not isinstance(status, dict)
                    or status.get("dir", "").replace("\\", "/") != directory):
                raise ProviderFailure("tool_response_invalid")
            children = status.get("followedBy")
            if not children:
                total = int(status.get("totalLength", 0))
                completed = int(status.get("completedLength", 0))
                return {
                    "ok": True, "download_id": token, "status": status["status"],
                    "total_bytes": total, "completed_bytes": completed,
                    "progress_percent": round(completed * 100 / total, 2) if total else None,
                    "download_speed": int(status.get("downloadSpeed", 0)),
                    "error_code": status.get("errorCode", "0"), "save_directory": directory,
                }
            if (not isinstance(children, list) or len(children) != 1
                    or not re.fullmatch(r"[0-9a-f]{16}", str(children[0]))):
                raise ProviderFailure("tool_response_invalid")
            gid = children[0]
        raise ProviderFailure("tool_response_invalid")
    except (ValueError, KeyError, TypeError):
        return {"ok": False, "error_code": "tool_arguments_invalid" if request.action == "start"
                else "tool_response_invalid"}
    except ProviderFailure as exc:
        if not exc.code.startswith("tool_"):
            raise
        # 提交超时可能已经生效，保留签名 ID 供查询，不能宣称下载未发生。
        return {"ok": False, "error_code": exc.code, "download_id": token,
                "summary": "请用此下载 ID 查询状态；提交失败或超时不代表后台未开始下载。"}

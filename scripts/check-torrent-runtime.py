"""隔离 aria2 实测：自建 tracker、HTTPS 种子与本地 BT 做种端。

需安装 aria2 并能绑定 443，建议在 --network none 的一次性 Linux 容器运行。
唯一测试替换是将 fixture 域名固定解析到本地 HTTPS 服务；TLS 校验仍开启。
不修改产品网络策略、真实用户配置或数据库。
"""

import hashlib
import ipaddress
import json
import os
import secrets
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from app.core.config import Settings
from app.providers.client import ProviderFailure
from app.tools.http import pictures, torrent
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pydantic import SecretStr


def bencode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, dict):
        return b"d" + b"".join(bencode(k) + bencode(value[k]) for k in sorted(value)) + b"e"
    raise TypeError(type(value))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run(output: Path):
    if not shutil.which("aria2c"):
        raise RuntimeError("aria2c is required")
    processes, servers, logs = [], [], []
    original_resolver = pictures.resolve_public
    original_ca = os.environ.get("SSL_CERT_FILE")
    report = {"verified_at": datetime.now(UTC).astimezone(
        tz=timezone(timedelta(hours=8)),
    ).isoformat(), "aria2_version": subprocess.check_output(
        ["aria2c", "--version"], text=True,
    ).splitlines()[0], "scope": "isolated local tracker + HTTPS + real BitTorrent peers"}
    with tempfile.TemporaryDirectory(prefix="torrent-smoke-") as work:
        root = Path(work)
        try:
            content = secrets.token_bytes(512 * 1024)
            name = "fixture.bin"
            seed_dir = root / "seed"
            seed_dir.mkdir()
            (seed_dir / name).write_bytes(content)
            seed_port = free_port()

            class Tracker(BaseHTTPRequestHandler):
                def do_GET(self):
                    body = bencode({b"interval": 1, b"peers": (
                        socket.inet_aton("127.0.0.1") + struct.pack("!H", seed_port)
                    )})
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *args):
                    pass

            def serve(handler, port=0):
                server = ThreadingHTTPServer(("127.0.0.1", port), handler)
                servers.append(server)
                return server

            tracker = serve(Tracker)
            threading.Thread(target=tracker.serve_forever, daemon=True).start()
            announce = f"http://127.0.0.1:{tracker.server_port}/announce"
            piece_length = 16384
            info = {
                b"name": name.encode(), b"length": len(content), b"piece length": piece_length,
                b"private": 1,
                b"pieces": b"".join(hashlib.sha1(content[i:i + piece_length]).digest()
                                    for i in range(0, len(content), piece_length)),
            }
            metadata = bencode({b"announce": announce.encode(), b"info": info})
            torrent_file = root / "fixture.torrent"
            torrent_file.write_bytes(metadata)

            class Metadata(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-bittorrent")
                    self.end_headers()
                    self.wfile.write(metadata)

                def log_message(self, *args):
                    pass

            https = serve(Metadata, 443)
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "torrent.fixture.test")])
            certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                           .public_key(key.public_key()).serial_number(x509.random_serial_number())
                           .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
                           .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
                           .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
                           .add_extension(x509.SubjectAlternativeName([
                               x509.DNSName("torrent.fixture.test"),
                               x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                           ]), False).sign(key, hashes.SHA256()))
            cert_file, key_file = root / "cert.pem", root / "key.pem"
            cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert_file, key_file)
            https.socket = context.wrap_socket(https.socket, server_side=True)
            threading.Thread(target=https.serve_forever, daemon=True).start()
            os.environ["SSL_CERT_FILE"] = str(cert_file)

            secret = secrets.token_hex(24)

            def start_aria(label, directory, port, extra):
                rpc_port = free_port()
                log = (root / f"{label}.log").open("w+")
                logs.append(log)
                process = subprocess.Popen([
                    "aria2c", "--no-conf=true", "--enable-rpc=true", "--rpc-listen-all=false",
                    f"--rpc-listen-port={rpc_port}", f"--rpc-secret={secret}",
                    "--rpc-max-request-size=8M", f"--listen-port={port}",
                    "--enable-dht=false", "--enable-dht6=false", "--bt-enable-lpd=false",
                    "--enable-peer-exchange=false", "--console-log-level=warn",
                    f"--dir={directory}", *extra,
                ], stdout=log, stderr=subprocess.STDOUT)
                processes.append(process)
                settings = Settings(_env_file=None, aria2_rpc_url=(
                    f"http://127.0.0.1:{rpc_port}/jsonrpc"),
                    aria2_rpc_secret=SecretStr(secret), torrent_download_root=str(directory))
                for _ in range(50):
                    if process.poll() is not None:
                        log.seek(0)
                        raise RuntimeError(log.read())
                    try:
                        torrent.rpc(settings, "getVersion", [])
                        return settings
                    except ProviderFailure:
                        time.sleep(0.1)
                raise TimeoutError("aria2 RPC startup")

            start_aria("seed", seed_dir, seed_port, [
                "--seed-ratio=0", "--seed-time=10", "--check-integrity=true", str(torrent_file),
            ])
            settings = start_aria("download", root / "downloads", free_port(), [
                "--seed-time=0", "--max-download-limit=128K",
            ])
            denied = torrent.download_torrent({"action": "start", "source": (
                "https://127.0.0.1/private.torrent")}, settings, "alice", "private", lambda: None)
            assert denied["error_code"] == "tool_network_denied", denied
            report["private_url_denied"] = True

            def resolve_fixture(host, deadline):
                if host == "torrent.fixture.test":
                    return "127.0.0.1"
                return original_resolver(host, deadline)

            pictures.resolve_public = resolve_fixture
            report["fixture_override"] = "Only HTTPS fixture DNS; TLS verification enabled"
            results = []
            for kind, source in [
                ("torrent", "https://torrent.fixture.test/file.torrent"),
                ("magnet", "magnet:?xt=urn:btih:" + hashlib.sha1(bencode(info)).hexdigest()
                 + "&tr=" + quote(announce, safe="")),
            ]:
                # 同一引擎不能同时载入相同 info hash；完成一项后移除其结果记录。
                submitted = torrent.download_torrent(
                    {"action": "start", "source": source}, settings, "alice", kind, lambda: None,
                )
                assert submitted.get("ok"), submitted
                duplicate = torrent.download_torrent(
                    {"action": "start", "source": source}, settings, "alice", kind, lambda: None,
                )
                assert not duplicate["ok"] and duplicate["download_id"] == submitted["download_id"]
                args = {"action": "status", "download_id": submitted["download_id"]}
                assert torrent.download_torrent(args, settings, "bob", "query", lambda: None)[
                    "error_code"] == "tool_not_authorized"
                states = []
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    result = torrent.download_torrent(
                        args, settings, "alice", "query", lambda: None,
                    )
                    assert result.get("ok"), result
                    if result["status"] not in states:
                        states.append(result["status"])
                    if result["status"] == "complete":
                        break
                    assert result["status"] != "error", result
                    time.sleep(0.25)
                else:
                    raise TimeoutError(f"{kind} download: {result}")
                downloaded = Path(result["save_directory"]) / name
                assert downloaded.read_bytes() == content
                assert result["progress_percent"] == 100
                results.append({"source": kind, "states": states, "completed_bytes": len(content),
                                "sha256": hashlib.sha256(content).hexdigest(),
                                "cross_user_denied": True, "duplicate_gid_rejected": True})
                # 删除已结束记录，保留文件，下一项使用独立目录。
                stopped = torrent.rpc(settings, "tellStopped", [0, 100, ["gid"]])
                for item in stopped:
                    torrent.rpc(settings, "removeDownloadResult", [item["gid"]])
            report["downloads"] = results
            report["ok"] = True
        except BaseException:
            for log in logs:
                log.flush()
                log.seek(0)
                print(log.read()[-6000:], file=sys.stderr)
            raise
        finally:
            pictures.resolve_public = original_resolver
            if original_ca is None:
                os.environ.pop("SSL_CERT_FILE", None)
            else:
                os.environ["SSL_CERT_FILE"] = original_ca
            for process in processes:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for server in servers:
                server.shutdown()
                server.server_close()
            for log in logs:
                log.close()
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    run(Path(sys.argv[1]))

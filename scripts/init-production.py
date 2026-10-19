"""初始化项目内部生产数据和私有配置，不生成或覆盖正式环境密钥。"""

import argparse
import ipaddress
import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def validate_storage(
    values: dict[str, str | None], local_test: bool = False
) -> dict[str, object]:
    """校验共用端点，正式部署要求HTTPS及预先创建的两个专用Bucket。"""
    endpoint = urlsplit(values.get("STORAGE_ENDPOINT") or "")
    host = values.get("S3_HOST") or ""
    port = values.get("S3_PORT") or "443"
    secure = values.get("S3_USE_SSL") or "true"
    fixture = (
        local_test
        and values.get("PROD_DATA_ROOT") == "data/stage6-production"
        and (values.get("PUBLIC_ORIGIN") or "").startswith("https://localhost:")
        and endpoint.hostname == host == "storage"
        and endpoint.scheme == "http"
        and port == "9000"
        and secure == "false"
    )
    if (
        not endpoint.hostname
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
        or endpoint.path not in {"", "/"}
        or host != endpoint.hostname
        or not port.isdigit()
        or not 1 <= int(port) <= 65535
        or (endpoint.port or (443 if endpoint.scheme == "https" else 80)) != int(port)
        or (not fixture and (endpoint.scheme != "https" or secure != "true"))
    ):
        raise ValueError("S3必须为无凭证的HTTPS端点，主机、端口与SSL配置一致")
    buckets = [
        values.get(name) or "" for name in ("STORAGE_BUCKET", "MILVUS_STORAGE_BUCKET")
    ]
    if any(
        not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
        for bucket in buckets
    ):
        raise ValueError("必须填写两个有效的专用S3 Bucket名称")
    if buckets[0] == buckets[1]:
        raise ValueError("原文和Milvus必须使用不同的专用Bucket")
    region = values.get("STORAGE_REGION") or "us-east-1"
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", region):
        raise ValueError("STORAGE_REGION格式无效")
    style = values.get("STORAGE_ADDRESSING_STYLE") or "path"
    if style not in {"path", "virtual"}:
        raise ValueError("STORAGE_ADDRESSING_STYLE只能为path或virtual")
    prefix = values.get("MILVUS_STORAGE_PREFIX") or "yugitalk-milvus"
    if not re.fullmatch(r"[a-zA-Z0-9_/-]{1,128}", prefix) or ".." in prefix:
        raise ValueError("MILVUS_STORAGE_PREFIX格式无效")
    ca_source = values.get("S3_CA_SOURCE") or ""
    ca_file = values.get("STORAGE_CA_FILE") or ""
    if bool(ca_source) != bool(ca_file) or (
        ca_file and ca_file != "/etc/yugitalk/s3/ca.pem"
    ):
        raise ValueError("私有CA需要同时配置S3_CA_SOURCE和固定容器CA路径")
    if ca_source:
        from cryptography import x509

        source = (ROOT / ca_source).resolve(strict=True)
        if not source.is_file() or not x509.load_pem_x509_certificates(
            source.read_bytes()
        ):
            raise ValueError("S3私有CA必须是有效的PEM证书文件")
    return {
        "address": host,
        "port": int(port),
        "useSSL": not fixture,
        "bucketName": buckets[1],
        "rootPath": prefix,
        "region": region,
        "useVirtualHost": style == "virtual",
        "ssl": {"tlsCACert": ca_file},
    }


def project_directory(relative: str) -> Path:
    """只允许项目 data 下的真实目录，避免生产挂载逃逸或复用开发数据。"""
    target = ROOT / relative
    if Path(relative).is_absolute() or not target.resolve().is_relative_to(
        ROOT / "data"
    ):
        raise ValueError("PROD_DATA_ROOT 必须是项目 data 下的相对目录")
    forbidden = [
        ROOT / "data" / name
        for name in ("postgres", "redis", "minio", "etcd", "milvus", "stage0")
    ]
    if target.resolve() == (ROOT / "data").resolve() or any(
        target.resolve().is_relative_to(path.resolve()) for path in forbidden
    ):
        raise ValueError("生产数据必须使用独立目录")
    for path in [target, *target.parents]:
        if path == ROOT.parent:
            break
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("生产数据目录不允许重解析路径")
    return target


def write_private(path: Path, content: str) -> None:
    """只创建不存在的私有配置；已存在时核对内容，禁止静默变更服务认证。"""
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise ValueError("私有配置已存在且内容不同，请执行明确的凭证轮换流程")
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)


def test_certificate(directory: Path) -> None:
    """为 localhost 隔离验收生成短期证书；正式域名必须使用受信任证书。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    if any(directory.iterdir()):
        raise ValueError("TLS 目录非空，禁止覆盖已有证书")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=7))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.DNSName("model-fixture"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    (directory / "fullchain.pem").write_bytes(
        certificate.public_bytes(serialization.Encoding.PEM)
    )
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    write_private(directory / "privkey.pem", private)


def validate_values(
    values: dict[str, str | None], certificate: bool = False
) -> tuple[Path, dict[str, object]]:
    """只读校验生产路径、凭证和存储声明，不创建目录或改动配置。"""
    root = project_directory(values.get("PROD_DATA_ROOT") or "data/production")
    origin = values.get("PUBLIC_ORIGIN") or ""
    if not re.fullmatch(r"https://[a-zA-Z0-9.-]+(?::[0-9]{1,5})?", origin):
        raise ValueError("PUBLIC_ORIGIN 必须是明确的 HTTPS 域名来源")
    storage = validate_storage(dict(values), local_test=certificate)
    names = [
        "POSTGRES_PASSWORD",
        "REDIS_PASSWORD",
        "MILVUS_PASSWORD",
        "SESSION_SECRET",
        "METRICS_TOKEN",
        "EMBEDDING_API_KEY",
    ]
    for name in names:
        value = values.get(name) or ""
        # 简化 URL、Compose 与服务配置转义，避免特殊字符改变配置语义。
        if (
            len(value) < 32
            or not value.isascii()
            or not all(c.isalnum() or c in "_-" for c in value)
        ):
            raise ValueError(f"{name} 使用至少32位字母数字或下划线短横线")
        if value.startswith("dev-") or value.lower() in {"milvus", "minioadmin"}:
            raise ValueError(f"{name} 不允许开发凭证")
    # 服务方凭证可以包含斜线、加号、等号，不套用本项目生成密码的规则。
    for name, minimum in (("STORAGE_ACCESS_KEY", 8), ("STORAGE_SECRET_KEY", 16)):
        value = values.get(name) or ""
        if (
            len(value) < minimum
            or not value.isascii()
            or any(ord(c) < 33 or ord(c) > 126 for c in value)
        ):
            raise ValueError(f"{name} 必须填写有效的服务方凭证")
        if value.startswith("dev-") or value == "minioadmin":
            raise ValueError(f"{name} 不允许开发凭证")
    return root, storage


def initialize(env_file: Path, certificate: bool = False) -> Path:
    """检查生产凭证与安全字符后创建目录及配置，原数据保持不变。"""
    values = dict(dotenv_values(env_file, interpolate=False))
    root, storage = validate_values(values, certificate)
    for folder in (
        "postgres",
        "redis",
        "minio",
        "etcd",
        "milvus",
        "config",
        "tls",
        "s3-ca",
        "prometheus",
        "jaeger",
        "backups",
    ):
        (root / folder).mkdir(parents=True, exist_ok=True)
    password = values["MILVUS_PASSWORD"]
    write_private(
        root / "config/milvus.yaml",
        "common:\n  security:\n    authorizationEnabled: true\n    enablePublicPrivilege: false\n    defaultRootPassword: "
        + json.dumps(password)
        + "\nminio: "
        + json.dumps(storage, ensure_ascii=False)
        + "\n",
    )
    if values.get("S3_CA_SOURCE"):
        write_private(
            root / "s3-ca/ca.pem",
            (ROOT / str(values["S3_CA_SOURCE"])).read_text(encoding="utf-8"),
        )
        (root / "s3-ca/ca.pem").chmod(0o644)
    write_private(
        root / "config/redis.conf",
        "appendonly yes\nprotected-mode yes\nmaxmemory 256mb\nmaxmemory-policy noeviction\nrequirepass "
        + str(values["REDIS_PASSWORD"])
        + "\n",
    )
    write_private(root / "config/metrics-token", str(values["METRICS_TOKEN"]) + "\n")
    nginx = (ROOT / "deploy/nginx.production.conf").read_text(encoding="utf-8")
    write_private(
        root / "config/nginx.conf", nginx.replace("__PUBLIC_ORIGIN__", origin)
    )
    if certificate:
        if not (values.get("PUBLIC_ORIGIN") or "").startswith("https://localhost:"):
            raise ValueError("测试证书只允许隔离的 localhost 入口")
        test_certificate(root / "tls")
    return root


def configure_permissions(root: Path, image: str) -> None:
    """使用隔离 Linux 工具容器设置绑定目录属主，保障非 root 服务访问。"""
    script = (
        "import os,pathlib; r=pathlib.Path('/data'); "
        "os.chown(r/'prometheus',65534,65534); os.chmod(r/'prometheus',0o750); "
        "os.chown(r/'jaeger',10001,10001); os.chmod(r/'jaeger',0o750); "
        "os.chown(r/'config/metrics-token',65534,65534); os.chmod(r/'config/metrics-token',0o600); "
        "os.chown(r/'config/nginx.conf',101,101); os.chmod(r/'config/nginx.conf',0o640); "
        "os.chown(r/'tls/privkey.pem',101,101); os.chmod(r/'tls/privkey.pem',0o600)"
    )
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--user",
            "0",
            "--mount",
            f"type=bind,source={root},target=/data",
            "--entrypoint",
            "python",
            image,
            "-c",
            script,
        ],
        check=True,
        timeout=60,
    )


def main() -> None:
    """从明确的生产环境文件初始化，不回显任何凭证。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--test-certificate", action="store_true")
    parser.add_argument(
        "--permissions", action="store_true", help="使用后端镜像初始化非root目录权限"
    )
    args = parser.parse_args()
    root = initialize(args.env_file, args.test_certificate)
    if args.permissions:
        values = dotenv_values(args.env_file)
        configure_permissions(
            root, values.get("BACKEND_IMAGE") or "yugitalk-backend:0.2.0"
        )
    print("生产数据与私有配置已初始化：" + str(root))


if __name__ == "__main__":
    main()

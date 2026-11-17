"""生成可选邮件告警的私有配置；不启动服务、不连接SMTP或发送通知。"""

import argparse
import json
import os
import re
import runpy
import tempfile
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def email_configuration(values: dict[str, str | None]) -> dict[str, object]:
    """校验单个收件人与SMTP声明，强制TLS并生成聚合及恢复通知配置。"""
    recipient = values.get("ALERT_EMAIL_TO") or ""
    sender = values.get("SMTP_SENDER") or ""
    for name, address in (("ALERT_EMAIL_TO", recipient), ("SMTP_SENDER", sender)):
        if not re.fullmatch(r"[^@\s,<>{}]+@[^@\s,<>{}]+\.[^@\s,<>{}]+", address):
            raise ValueError(name + "必须填写单个邮箱地址，不支持模板或显示名称")
    host = values.get("SMTP_HOST") or ""
    if not re.fullmatch(r"[a-zA-Z0-9.-]+", host):
        raise ValueError("SMTP_HOST必须填写邮件服务器主机名")
    try:
        port = int(values.get("SMTP_PORT") or "465")
    except ValueError:
        raise ValueError("SMTP_PORT必须是有效端口") from None
    if not 1 <= port <= 65535:
        raise ValueError("SMTP_PORT必须是有效端口")
    tls = values.get("SMTP_TLS") or "ssl"
    if tls not in {"ssl", "starttls"}:
        raise ValueError("SMTP_TLS只支持ssl或starttls")
    username = values.get("SMTP_USERNAME") or ""
    password = values.get("SMTP_PASSWORD") or ""
    if not username or not password:
        raise ValueError("邮件告警必须配置SMTP_USERNAME和SMTP_PASSWORD")
    return {
        "global": {
            "smtp_smarthost": f"{host}:{port}",
            "smtp_from": sender,
            "smtp_auth_username": username,
            "smtp_auth_password": password,
            "smtp_require_tls": True,
            "smtp_force_implicit_tls": tls == "ssl",
        },
        "route": {
            "receiver": "operations-email",
            "group_by": ["alertname", "severity"],
            "group_wait": "30s",
            "group_interval": "5m",
            "repeat_interval": "4h",
        },
        "receivers": [
            {
                "name": "operations-email",
                "email_configs": [{"to": recipient, "send_resolved": True}],
            }
        ],
    }


def write_configuration(path: Path, content: str, mode: int) -> None:
    """原子替换本功能配置，临时文件保持私有，写入失败不留下半个配置。"""
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def configure(env_file: Path, check_only: bool = False) -> Path:
    """复用生产目录边界，只生成告警专用文件；预检不创建任何目录。"""
    if not env_file.is_file():
        raise ValueError("必须指定存在的私有生产环境文件")
    values = dotenv_values(env_file, interpolate=False)
    config = email_configuration(values)
    initializer = runpy.run_path(str(ROOT / "scripts/init-production.py"))
    relative = values.get("PROD_DATA_ROOT") or "data/production"
    initializer["project_directory"](relative)
    directory = initializer["project_directory"](str(Path(relative) / "config"))
    target = directory / "alertmanager.json"
    prometheus = (ROOT / "deploy/prometheus.yaml").read_text(encoding="utf-8")
    prometheus += (
        "\nalerting:\n"
        "  alertmanagers:\n"
        "    - static_configs:\n"
        "        - targets: [alertmanager:9093]\n"
    )
    if not check_only:
        directory.mkdir(parents=True, exist_ok=True)
        write_configuration(
            target, json.dumps(config, ensure_ascii=False, indent=2) + "\n", 0o600
        )
        write_configuration(directory / "prometheus-alerting.yaml", prometheus, 0o644)
    return target


def main() -> int:
    """CLI只输出状态和异常类别，任何私有地址或凭证均不进入终端。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        configure(args.env_file, check_only=args.check)
    except (ValueError, OSError) as error:
        print("告警配置失败：" + type(error).__name__)
        return 1
    print(
        "告警配置预检通过，未写入。"
        if args.check
        else "告警配置已生成，请按docs/ALERTING.md启用。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

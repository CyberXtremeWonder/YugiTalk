"""验证告警配置生成和输入边界，测试不连接SMTP或发送通知。"""

import json
import runpy
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def email_values() -> dict[str, str]:
    """使用虚构邮件地址和凭证构造有效的告警配置。"""
    return {
        "ALERT_EMAIL_TO": "operations@example.org",
        "SMTP_HOST": "smtp.example.org",
        "SMTP_PORT": "465",
        "SMTP_TLS": "ssl",
        "SMTP_USERNAME": "sender@example.org",
        "SMTP_PASSWORD": "test-password-only",
        "SMTP_SENDER": "sender@example.org",
    }


@pytest.mark.parametrize("tls,implicit", [("ssl", True), ("starttls", False)])
def test_email_transport_requires_tls(tls: str, implicit: bool) -> None:
    """显式保留两种TLS方式，恢复通知及聚合间隔由配置约束。"""
    script = runpy.run_path(str(ROOT / "scripts/configure-alerts.py"))
    config = script["email_configuration"](email_values() | {"SMTP_TLS": tls})
    assert config["global"]["smtp_require_tls"] is True
    assert config["global"]["smtp_force_implicit_tls"] is implicit
    assert config["global"]["smtp_smarthost"] == "smtp.example.org:465"
    assert config["route"]["repeat_interval"] == "4h"
    receiver = config["receivers"][0]
    assert receiver["name"] == config["route"]["receiver"]
    assert receiver["email_configs"][0]["send_resolved"] is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"ALERT_EMAIL_TO": ""},
        {"ALERT_EMAIL_TO": "invalid"},
        {"ALERT_EMAIL_TO": "user@example.org\nBcc: other@example.org"},
        {"ALERT_EMAIL_TO": "{{ .CommonLabels.user }}@example.org"},
        {"SMTP_HOST": "https://smtp.example.org"},
        {"SMTP_PORT": "0"},
        {"SMTP_PORT": "65536"},
        {"SMTP_PORT": "invalid"},
        {"SMTP_TLS": "none"},
        {"SMTP_USERNAME": ""},
        {"SMTP_PASSWORD": ""},
        {"SMTP_SENDER": ""},
    ],
)
def test_invalid_notification_configuration_refused(overrides: dict[str, str]) -> None:
    """缺少收件人、凭证或TLS时拒绝生成，错误不回显私有值。"""
    script = runpy.run_path(str(ROOT / "scripts/configure-alerts.py"))
    with pytest.raises(ValueError) as failure:
        script["email_configuration"](email_values() | overrides)
    assert "test-password-only" not in str(failure.value)


def test_configuration_preflight_and_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """预检无写入，实际生成使用项目目录边界且不输出邮件凭证。"""
    script = runpy.run_path(str(ROOT / "scripts/configure-alerts.py"))
    initializer = runpy.run_path(str(ROOT / "scripts/init-production.py"))
    monkeypatch.setitem(initializer["project_directory"].__globals__, "ROOT", tmp_path)
    monkeypatch.setitem(script["configure"].__globals__, "ROOT", tmp_path)
    monkeypatch.setattr(runpy, "run_path", lambda path: initializer)
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy/prometheus.yaml").write_text(
        (ROOT / "deploy/prometheus.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    values = email_values() | {"PROD_DATA_ROOT": "data/production"}
    environment = tmp_path / "private.env"
    environment.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()), encoding="utf-8"
    )
    target = script["configure"](environment, check_only=True)
    assert not (tmp_path / "data").exists()
    assert script["configure"](environment) == target
    config = json.loads(target.read_text(encoding="utf-8"))
    assert config["global"]["smtp_auth_password"] == "test-password-only"
    prometheus = yaml.safe_load(
        (target.parent / "prometheus-alerting.yaml").read_text(encoding="utf-8")
    )
    original = yaml.safe_load((ROOT / "deploy/prometheus.yaml").read_text(encoding="utf-8"))
    assert prometheus["scrape_configs"] == original["scrape_configs"]
    assert prometheus["alerting"]["alertmanagers"][0]["static_configs"] == [
        {"targets": ["alertmanager:9093"]}
    ]
    assert "test-password-only" not in capsys.readouterr().out

    original_environment = environment.read_text(encoding="utf-8")
    for invalid in ("../outside", "data", "data/postgres"):
        environment.write_text(
            original_environment.replace("data/production", invalid), encoding="utf-8"
        )
        with pytest.raises(ValueError):
            script["configure"](environment)
    assert json.loads(target.read_text(encoding="utf-8")) == config


def test_notification_cli_redacts_errors(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """CLI只打印失败类别，避免输入值出现在日志或终端历史中。"""
    script = runpy.run_path(str(ROOT / "scripts/configure-alerts.py"))

    def fail(*arguments: Any, **options: Any) -> Path:
        """模拟包含凭证的底层异常，确保调用者仍脱敏。"""
        raise ValueError("private-password-and-address")

    monkeypatch.setitem(script["main"].__globals__, "configure", fail)
    monkeypatch.setattr("sys.argv", ["configure-alerts.py", "--env-file", "private.env", "--check"])
    assert script["main"]() == 1
    assert "private-password-and-address" not in capsys.readouterr().out


def test_alerting_override_keeps_notifications_private() -> None:
    """覆盖文件只涉及监控，告警组件可出网发信但不发布公网端口。"""
    override = yaml.safe_load((ROOT / "docker-compose.alerts.yml").read_text(encoding="utf-8"))
    assert set(override["services"]) == {"alertmanager", "prometheus"}
    alertmanager = override["services"]["alertmanager"]
    assert not alertmanager.get("ports")
    assert set(alertmanager["networks"]) == {"default", "private"}
    assert alertmanager["user"] == "65534:65534"
    assert "@sha256:" in alertmanager["image"]
    assert alertmanager["read_only"] is True

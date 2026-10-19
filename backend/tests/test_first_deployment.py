"""验证首次部署流程与失败边界，使用替身避免访问Docker、云存储或真实模型。"""

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def starter() -> ModuleType:
    """按脚本路径加载部署入口，隔离每个测试对依赖的替换。"""
    spec = importlib.util.spec_from_file_location(
        "first_deployment", ROOT / "scripts/start-production.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_environment_file_wins(starter: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """主机残留密钥和隐式Compose选择不能覆盖明确的环境文件。"""
    monkeypatch.setenv("MODEL_API_KEY", "host-secret-not-for-this-project")
    monkeypatch.setenv("COMPOSE_PROFILES", "local-test-storage")
    monkeypatch.setenv("COMPOSE_FILE", "wrong.yml")
    environment = starter.clean_environment({"MODEL_API_KEY": "private-file-secret"})
    assert "MODEL_API_KEY" not in environment
    assert "COMPOSE_FILE" not in environment
    assert "COMPOSE_PROFILES" not in environment


@pytest.mark.parametrize("folder", ["postgres", "redis", "etcd", "milvus", "prometheus", "jaeger"])
def test_nonempty_target_refused(starter: ModuleType, tmp_path: Path, folder: str) -> None:
    """现有持久数据阻止首次部署，失败后原文件必须保持完整。"""
    directory = tmp_path / folder
    directory.mkdir()
    original = directory / "existing.data"
    original.write_bytes(b"preserve-this-data")
    with pytest.raises(ValueError, match="目标已有持久数据"):
        starter.check_empty_target(tmp_path)
    assert original.read_bytes() == b"preserve-this-data"


@pytest.mark.parametrize(
    "origin", ["https://localhost", "https://agent.example.com", "https://a.test"]
)
def test_example_certificate_origin_refused(
    starter: ModuleType, tmp_path: Path, origin: str
) -> None:
    """正式部署拒绝隔离或示例域名，不将自签测试入口当作上线证据。"""
    with pytest.raises(ValueError, match="实际部署域名"):
        starter.check_certificate(tmp_path, origin)


def make_plan(starter: ModuleType, tmp_path: Path, calls: list[Any]) -> dict[str, Any]:
    """构造只有虚构配置的启动计划，并记录初始化与权限操作顺序。"""
    (tmp_path / "config").mkdir()

    def initialize(env_file: Path) -> Path:
        """记录初始化操作，不创建数据库或访问远端服务。"""
        calls.append("initialize")
        return tmp_path

    def permissions(root: Path, image: str) -> None:
        """记录权限准备，避免实际启动工具容器。"""
        calls.append("permissions")

    def write_private(path: Path, content: str) -> None:
        """保存模拟固定镜像配置，供测试核对启动绑定的实际版本。"""
        path.write_text(content, encoding="utf-8")

    return {
        "values": {"PUBLIC_ORIGIN": "https://agent.deploy.example.org"},
        "env_file": tmp_path / "private.env",
        "environment": {},
        "images": {
            "api": "sha256:" + "a" * 64,
            "frontend": "sha256:" + "b" * 64,
            "milvus": "sha256:" + "c" * 64,
        },
        "compose": ["docker", "compose", "-p", "yugitalk-prod"],
        "initializer": {
            "initialize": initialize,
            "configure_permissions": permissions,
            "write_private": write_private,
        },
    }


@pytest.mark.parametrize("failure", [None, "s3", "start", "https"])
def test_first_deploy_records_actual_scope(
    starter: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    """成功保留范围声明，S3、启动或HTTPS失败都不能产生成功报告或执行恢复。"""
    calls: list[Any] = []
    plan = make_plan(starter, tmp_path, calls)

    def storage(env_file: Path, write_probe: bool) -> dict[str, str]:
        """记录两桶写入诊断请求，用受控失败替代真实服务故障。"""
        assert write_probe
        calls.append("s3")
        return {"status": "failed" if failure == "s3" else "passed"}

    def load_script(path: str) -> dict[str, Any]:
        """只允许加载存储探测脚本，确保首次部署不会进入备份恢复流程。"""
        assert path.endswith("check-production-s3.py")
        return {"check_storage": storage}

    def command(arguments: list[str], environment: dict[str, str]) -> str:
        """核对启动只使用已固定的镜像并拒绝隐式构建或拉取。"""
        calls.append("start")
        assert "--no-build" in arguments
        assert arguments[arguments.index("--pull") + 1] == "never"
        assert "first-images.compose.json" in " ".join(arguments)
        assert environment["BACKEND_IMAGE"] == plan["images"]["api"]
        if failure == "start":
            raise RuntimeError("simulated start failure")
        return ""

    def https(origin: str) -> None:
        """记录实际域名检查阶段，替代公网访问。"""
        calls.append("https")
        if failure == "https":
            raise RuntimeError("simulated https failure")

    monkeypatch.setattr(starter.runpy, "run_path", load_script)
    monkeypatch.setattr(starter, "command", command)
    monkeypatch.setattr(starter, "verify_https", https)
    if failure:
        with pytest.raises(RuntimeError):
            starter.deploy(plan, "yugitalk-prod")
    else:
        result = starter.deploy(plan, "yugitalk-prod")
        assert result["status"] == "deployed"
        assert calls == ["s3", "initialize", "permissions", "start", "https"]
    report = tmp_path / "first-deployment.json"
    if failure == "s3":
        assert calls == ["s3"]
        assert not report.exists()
    else:
        record = json.loads(report.read_text(encoding="utf-8"))
        assert record["restore_drill"] == "not_performed"
        assert record["backup_created"] is False
        assert record["model_quality_verified"] is False
        assert record["status"] == ("failed" if failure else "deployed")
        pins = json.loads(
            (tmp_path / "config/first-images.compose.json").read_text(encoding="utf-8")
        )
        assert pins["services"]["milvus"]["image"] == plan["images"]["milvus"]


def test_main_does_not_leak_private_error(
    starter: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """SDK或校验异常包含输入值时，CLI只输出异常类别。"""

    def fail(env_file: Path, project: str) -> dict[str, Any]:
        """制造可能携带密钥的配置异常，验证启动入口脱敏。"""
        raise ValueError("do-not-expose-secret-input")

    monkeypatch.setattr(starter, "prepare", fail)
    monkeypatch.setattr("sys.argv", ["start-production.py", "--env-file", "private.env", "--check"])
    assert starter.main() == 1
    assert "do-not-expose-secret-input" not in capsys.readouterr().out


@pytest.mark.parametrize("existing", [False, True])
def test_preflight_has_no_deployment_side_effects(
    starter: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    """完整预检只读取Docker元数据，既有项目在启动之前被拒绝且不生成业务文件。"""
    root = tmp_path / "data/production"
    config = tmp_path / "private.env"
    config.write_text("EMBEDDING_BASE_URL=https://embedding.example.org/v1\n", encoding="utf-8")
    calls: list[list[str]] = []

    def validate(values: dict[str, str | None]) -> tuple[Path, dict[str, Any]]:
        """以虚构空目录代替已有项目的初始化校验，不创建目录。"""
        return root, {}

    def command(arguments: list[str], environment: dict[str, str]) -> str:
        """只接受配置、状态和镜像读取命令，任何启动或写操作立即使测试失败。"""
        calls.append(arguments)
        if "config" in arguments:
            return json.dumps(
                {
                    "services": {
                        "api": {
                            "image": "backend",
                            "environment": {
                                "APP_ENV": "production",
                                "MODEL_PROVIDER": "openai_compatible",
                                "MODEL_ID": "test-model",
                                "MODEL_BASE_URL": "https://model.example.org/v1",
                                "MODEL_API_KEY": "fake-model-key",
                                "MILVUS_TOKEN": "root:fake-vector-key",
                                "SESSION_SECRET": "s" * 40,
                                "METRICS_TOKEN": "m" * 40,
                                "DATABASE_URL": "postgresql://user:fake-pass@postgres/yugitalk",
                                "REDIS_URL": "redis://:fake-pass@redis/0",
                                "STORAGE_ENDPOINT": "https://s3.example.org",
                                "STORAGE_ACCESS_KEY": "fake-access",
                                "STORAGE_SECRET_KEY": "fake-secret",
                                "AUTH_TRUSTED_ORIGINS": '["https://agent.example.org"]',
                                "EMBEDDING_API_KEY": "fake-embedding-key",
                            },
                        },
                        "storage": {"profiles": ["local-test-storage"], "image": "must-not-start"},
                    }
                }
            )
        if "ps" in arguments:
            return "existing-container" if existing else ""
        assert arguments[:3] == ["docker", "image", "inspect"]
        assert "must-not-start" not in arguments
        return "sha256:" + "d" * 64

    monkeypatch.setattr(starter, "ROOT", tmp_path)
    monkeypatch.setattr(starter, "check_certificate", lambda *args: None)
    monkeypatch.setattr(starter.runpy, "run_path", lambda path: {"validate_values": validate})
    monkeypatch.setattr(starter, "command", command)
    if existing:
        with pytest.raises(ValueError, match="已有容器"):
            starter.prepare(config, "yugitalk-prod")
    else:
        plan = starter.prepare(config, "yugitalk-prod")
        assert set(plan["images"]) == {"api"}
    assert not root.exists()
    assert all("up" not in arguments and "run" not in arguments for arguments in calls)

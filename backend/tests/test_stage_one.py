"""验证配置边界、服务协议及启动预检，不依赖模型密钥。"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from app.core.config import Settings
from app.health.checks import DependencyChecker
from app.main import create_app
from fastapi.testclient import TestClient
from pydantic import ValidationError
from uvicorn import Config


def test_local_defaults_match_development_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺少 .env 时仍连接本项目的开发容器，避免误连标准端口的其他服务。"""
    for variable in ("DATABASE_URL", "REDIS_URL", "STORAGE_ENDPOINT", "MILVUS_URI", "APP_ENV"):
        monkeypatch.delenv(variable, raising=False)
    settings = Settings(_env_file=None, app_env="test")
    sample = (Path(__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8")
    compose = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    for address, port, container_port in (
        (settings.database_url.get_secret_value(), 25432, 5432),
        (settings.redis_url.get_secret_value(), 26379, 6379),
        (settings.storage_endpoint, 29000, 9000),
        (settings.milvus_uri, 29530, 19530),
    ):
        assert urlsplit(address).hostname == "127.0.0.1"
        assert urlsplit(address).port == port
        assert address in sample
        assert f"127.0.0.1:{port}:{container_port}" in compose


def test_database_event_loop_factory() -> None:
    """普通启动与热重载均不能选用 Psycopg 不支持的 Proactor 循环。"""
    for reload in (False, True):
        config = Config(
            "app.main:create_app", loop="app.core.event_loop:selector_loop_factory", reload=reload
        )
        factory = config.get_loop_factory()
        assert factory is not None
        loop = factory()
        try:
            assert isinstance(loop, asyncio.SelectorEventLoop)
        finally:
            loop.close()


@pytest.mark.parametrize(
    "checks,expected",
    [
        ({"postgres": True, "redis": True, "storage": True, "milvus": True}, 200),
        ({"postgres": True, "redis": False, "storage": True, "milvus": True}, 503),
        ({"postgres": True, "redis": True, "storage": True, "milvus": False}, 503),
    ],
)
def test_health_protocol(checks: dict[str, bool], expected: int) -> None:
    """验证健康接口返回预期的就绪状态与响应契约。"""

    async def checker() -> dict[str, bool]:
        """提供受控健康探测结果，隔离测试中的外部服务。"""
        return checks

    with TestClient(create_app(Settings(app_env="test"), checker)) as client:
        assert client.get("/health/live").status_code == 200
        response = client.get("/health/ready")
        assert response.status_code == expected
        assert response.json()["checks"] == checks
        assert len(response.headers["X-Request-ID"]) == 32
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        missing = client.get("/unknown?secret=do-not-repeat")
        assert missing.status_code == 404
        assert missing.json()["code"] == "not_found"
        assert "do-not-repeat" not in missing.text


@pytest.mark.parametrize(
    "overrides",
    [
        {"app_env": "production"},
        {"database_url": "invalid-secret"},
        {"app_port": 0},
        {"dependency_timeout_seconds": 0},
    ],
)
def test_invalid_configuration(overrides: dict[str, object]) -> None:
    """验证非法环境配置在启动前被拒绝。"""
    with pytest.raises(ValidationError):
        Settings.model_validate(overrides)


def test_secret_representation() -> None:
    """验证配置对象的公开表示不包含敏感信息。"""
    settings = Settings(app_env="test")
    assert "dev-database-only" not in repr(settings)
    assert "dev-storage-password" not in settings.model_dump_json()


def production_values() -> dict[str, object]:
    """只提供验证用虚构凭证，绝不连接外部模型或服务。"""
    return {
        "milvus_token": "test-user:test-password",
        "metrics_token": "m" * 40,
        "auth_trusted_origins": ["https://example.test"],
        "app_env": "production",
        "cumulative_budget_enabled": True,
        "task_budget_enabled": True,
        "embedding_api_key": "fake-embedding-validation-key",
        "model_provider": "openai_compatible",
        "model_id": "test-model",
        "model_base_url": "https://model.example.test/v1",
        "model_api_key": "fake-key-for-validation",
        "session_secret": "x" * 40,
        "database_url": "postgresql://user:test-password@localhost/test",
        "redis_url": "redis://:test-password@localhost/0",
        "storage_access_key": "test-user",
        "storage_endpoint": "https://storage.example.test",
        "storage_secret_key": "test-password",
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"app_reload": True},
        {"milvus_token": ""},
        {"log_level": "DEBUG"},
        {"model_api_key": ""},
        {"model_base_url": "http://model.example.test/v1"},
        {"model_base_url": "https://model.example.test/v1?key=private"},
        {"session_secret": "short"},
        {"cors_origins": ["*"]},
        {"redis_url": "redis://localhost/0"},
        {"storage_secret_key": "dev-placeholder"},
        {"storage_endpoint": "http://storage.example.test"},
        {"storage_local_test_fixture": True},
        {"storage_endpoint": "http://storage:9000", "storage_local_test_fixture": True},
    ],
)
def test_production_boundaries(overrides: dict[str, object]) -> None:
    """验证生产配置必须满足认证、密钥与调试限制。"""
    with pytest.raises(ValidationError):
        Settings.model_validate(production_values() | overrides)


def test_production_hides_docs() -> None:
    """验证生产环境关闭开发文档入口。"""
    with TestClient(create_app(Settings.model_validate(production_values()))) as client:
        root = client.get("/", follow_redirects=False)
        assert root.status_code == 307
        assert root.headers["location"] == "/health/live"
        assert client.get("/health/live").status_code == 200
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_production_local_storage_fixture_is_explicit_and_scoped() -> None:
    """仅明确的localhost隔离测试允许本机HTTP存储，正式默认仍要求HTTPS。"""
    values = production_values() | {
        "storage_endpoint": "http://storage:9000",
        "storage_local_test_fixture": True,
        "auth_trusted_origins": ["https://localhost:18443"],
    }
    assert Settings.model_validate(values).storage_local_test_fixture


def test_development_root_opens_api_docs() -> None:
    """验证开发环境根路径引导到接口文档。"""
    with TestClient(create_app(Settings(app_env="test"))) as client:
        root = client.get("/", follow_redirects=False)
        assert root.status_code == 307
        assert root.headers["location"] == "/docs"
        assert client.get("/").status_code == 200


def test_unexpected_errors_are_redacted() -> None:
    """验证未知异常返回统一脱敏错误，而不暴露内部详情。"""

    async def broken() -> dict[str, bool]:
        """模拟未预料的接口异常，验证错误详情不会泄露。"""
        raise RuntimeError("secret-connection-value")

    with TestClient(create_app(Settings(app_env="test"), broken)) as client:
        response = client.get("/health/ready")
        assert response.status_code == 500
        assert response.json()["code"] == "internal_error"
        assert "secret-connection-value" not in response.text


def test_probe_failure_and_timeout() -> None:
    """验证依赖探测故障和超时互相隔离。"""
    checker = DependencyChecker(Settings(app_env="test", dependency_timeout_seconds=0.01))

    async def failing() -> None:
        """提供失败的依赖探测，验证健康接口的异常隔离。"""
        raise RuntimeError("sensitive-connection")

    async def slow() -> None:
        """提供超时的依赖探测，验证等待时间受到上限约束。"""
        await asyncio.sleep(0.1)

    assert asyncio.run(checker._safe_check(failing)) is False
    assert asyncio.run(checker._safe_check(slow)) is False


def test_start_preflight() -> None:
    """验证统一启动入口的配置预检与失败退出码。"""
    environment = dict(os.environ, APP_ENV="test", PYTHONUTF8="1")
    valid = subprocess.run(
        [sys.executable, "start.py", "--check"],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=Path(__file__).resolve().parents[1],
    )
    assert valid.returncode == 0
    assert '"status": "valid"' in valid.stdout
    invalid = subprocess.run(
        [sys.executable, "start.py", "--env", "production", "--check"],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=Path(__file__).resolve().parents[1],
    )
    assert invalid.returncode == 2
    assert "dev-database-only" not in invalid.stderr


@pytest.mark.parametrize(
    "field,value",
    [
        ("TOOL_HTTP_ALLOWED_HOSTS", "['restapi.amap.com']"),
        ("TOOL_SECRETS", "{'PRIVATE_TOKEN': 'do-not-expose-config-secret'}"),
    ],
)
@pytest.mark.parametrize("source", ["environment", "dotenv"])
def test_start_json_configuration_error(
    field: str, value: str, source: str, tmp_path: Path
) -> None:
    """两种配置来源格式错误均指出字段，不泄漏密钥或原始配置值。"""
    environment = dict(os.environ, APP_ENV="test", PYTHONUTF8="1")
    environment.pop(field, None)
    if source == "environment":
        environment[field] = value
        command = [sys.executable, "start.py", "--check"]
    else:
        config_file = tmp_path / "invalid.env"
        config_file.write_text(f"{field}={value}\n", encoding="utf-8")
        program = (
            "import sys; from app.core.config import Settings; "
            "Settings.model_config['env_file'] = sys.argv[1]; "
            "import start; sys.argv = ['start.py', '--check']; "
            "raise SystemExit(start.main())"
        )
        command = [sys.executable, "-c", program, str(config_file)]
    result = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode == 2
    assert f"{field}: JSON 格式无效" in result.stderr
    assert "英文双引号" in result.stderr
    assert "do-not-expose-config-secret" not in result.stderr + result.stdout
    assert "Traceback" not in result.stderr
    if field == "TOOL_HTTP_ALLOWED_HOSTS":
        assert 'TOOL_HTTP_ALLOWED_HOSTS=["restapi.amap.com"]' in result.stderr


def test_database_initialization_registers_worker_entities() -> None:
    """验证独立进程无需加载 API 也能解析全部外键，防止任务永远停留在排队状态。"""
    # 必须启动新进程；当前测试进程已加载 API，无法重现遗漏实体注册的问题。
    program = """
import sys
from sqlalchemy.orm import configure_mappers
from app.core.config import Settings
from app.core.database import create_database
from app.core.entities import Base

assert 'app.main' not in sys.modules
engine = create_database(Settings(_env_file=None))
configure_mappers()
assert {'users', 'agent_versions', 'runs', 'documents'} <= Base.metadata.tables.keys()
for table in Base.metadata.tables.values():
    for key in table.foreign_keys:
        assert key.column.table.name in Base.metadata.tables
engine.dispose()
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr

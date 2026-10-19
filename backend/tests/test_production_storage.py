"""覆盖现有S3的配置边界与权限约束，测试不访问网络或真实账户。"""

import runpy
from io import BytesIO
from pathlib import Path
from typing import Any

import boto3
import pytest
from app.core.config import Settings
from app.rag.storage import ObjectStorage
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import Stubber

ROOT = Path(__file__).resolve().parents[2]


def s3_values() -> dict[str, str]:
    """构造没有真实凭证的正式S3配置边界样例。"""
    return {
        "STORAGE_ENDPOINT": "https://s3.example.test:9443",
        "S3_HOST": "s3.example.test",
        "S3_PORT": "9443",
        "S3_USE_SSL": "true",
        "STORAGE_BUCKET": "yugitalk-documents",
        "MILVUS_STORAGE_BUCKET": "yugitalk-vectors",
        "STORAGE_REGION": "eu-west-1",
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"STORAGE_ENDPOINT": ""},
        {"STORAGE_ENDPOINT": "http://s3.example.test:9443"},
        {"S3_HOST": "another.example.test"},
        {"S3_PORT": "443"},
        {"S3_USE_SSL": "false"},
        {"STORAGE_BUCKET": "yugitalk-vectors"},
        {"MILVUS_STORAGE_BUCKET": ""},
        {"STORAGE_ENDPOINT": "https://credential@s3.example.test:9443"},
        {"MILVUS_STORAGE_PREFIX": "../other"},
        {"STORAGE_ADDRESSING_STYLE": "invalid"},
        {"STORAGE_CA_FILE": "/unmounted/ca.pem"},
    ],
)
def test_external_s3_rejects_inconsistent_configuration(overrides: dict[str, str]) -> None:
    """不完整或不一致的正式配置必须在创建数据目录与服务之前失败。"""
    initializer = runpy.run_path(str(ROOT / "scripts/init-production.py"))
    with pytest.raises(ValueError):
        initializer["validate_storage"](s3_values() | overrides)


def test_external_s3_generates_explicit_milvus_settings() -> None:
    """Milvus从user.yaml读取端口、SSL、Bucket和Region，避免依赖未声明环境变量。"""
    initializer = runpy.run_path(str(ROOT / "scripts/init-production.py"))
    settings = initializer["validate_storage"](
        s3_values() | {"STORAGE_ADDRESSING_STYLE": "virtual"}
    )
    assert settings["port"] == 9443 and settings["useSSL"] is True
    assert settings["bucketName"] == "yugitalk-vectors"
    assert settings["useVirtualHost"] is True and settings["region"] == "eu-west-1"


def test_s3_local_fixture_exception_is_strictly_scoped() -> None:
    """HTTP测试例外仅允许明确的隔离目录和localhost入口，不可套用正式目录。"""
    initializer = runpy.run_path(str(ROOT / "scripts/init-production.py"))
    fixture = s3_values() | {
        "STORAGE_ENDPOINT": "http://storage:9000",
        "S3_HOST": "storage",
        "S3_PORT": "9000",
        "S3_USE_SSL": "false",
        "PROD_DATA_ROOT": "data/stage6-production",
        "PUBLIC_ORIGIN": "https://localhost:18443",
    }
    assert initializer["validate_storage"](fixture, True)["useSSL"] is False
    with pytest.raises(ValueError):
        initializer["validate_storage"](fixture | {"PROD_DATA_ROOT": "data/production"}, True)


def make_storage(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> tuple[ObjectStorage, Any, dict[str, Any]]:
    """使用真实SDK的Stubber检查请求序列，并捕获客户端的TLS和Region参数。"""
    client = boto3.client(
        "s3",
        region_name="eu-west-1",
        aws_access_key_id="test-key",
        aws_secret_access_key="test-secret",
        endpoint_url="https://s3.example.test",
    )
    captured: dict[str, Any] = {}

    def factory(service: str, **arguments: Any) -> Any:
        """替换SDK工厂并保留构造选项，所有业务请求由Stubber接管。"""
        assert service == "s3"
        captured.update(arguments)
        return client

    monkeypatch.setattr("app.rag.storage.boto3.client", factory)
    return ObjectStorage(settings), client, captured


def test_production_storage_never_creates_missing_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """正式缺桶时直接失败，应用不尝试创建Bucket或升级权限。"""
    settings = Settings(_env_file=None, app_env="test").model_copy(update={"app_env": "production"})
    storage, client, captured = make_storage(monkeypatch, settings)
    assert captured["verify"] is True
    with Stubber(client) as stub:
        stub.add_client_error(
            "head_bucket",
            service_error_code="404",
            http_status_code=404,
            expected_params={"Bucket": settings.storage_bucket},
        )
        with pytest.raises(ClientError):
            storage.put("documents/test", b"example")
        stub.assert_no_pending_responses()


def test_development_bucket_uses_configured_region(monkeypatch: pytest.MonkeyPatch) -> None:
    """本机缺桶可创建，非us-east-1区必须携带LocationConstraint。"""
    settings = Settings(
        _env_file=None,
        app_env="test",
        storage_region="eu-west-1",
        storage_addressing_style="virtual",
    ).model_copy(update={"storage_ca_file": "/ca.pem"})
    storage, client, captured = make_storage(monkeypatch, settings)
    assert captured["verify"] == "/ca.pem" and captured["region_name"] == "eu-west-1"
    assert captured["config"].s3["addressing_style"] == "virtual"
    with Stubber(client) as stub:
        stub.add_client_error(
            "head_bucket",
            service_error_code="404",
            http_status_code=404,
            expected_params={"Bucket": settings.storage_bucket},
        )
        stub.add_response(
            "create_bucket",
            {},
            {
                "Bucket": settings.storage_bucket,
                "CreateBucketConfiguration": {"LocationConstraint": "eu-west-1"},
            },
        )
        stub.add_response(
            "put_object",
            {},
            {
                "Bucket": settings.storage_bucket,
                "Key": "documents/test",
                "Body": b"example",
                "ContentType": "application/octet-stream",
            },
        )
        storage.put("documents/test", b"example")
        stub.assert_no_pending_responses()


def test_external_s3_backup_refuses_before_service_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未实现远端对象备份时，在任何Docker或停机命令之前明确拒绝。"""
    environment = tmp_path / "production.env"
    environment.write_text("STORAGE_ENDPOINT=https://s3.example.test\n", encoding="utf-8")
    script = runpy.run_path(str(ROOT / "scripts/production-data.py"))
    invoked = []

    def forbidden_command(*arguments: Any, **options: Any) -> str:
        """捕获不应发生的外部操作，防止测试误操作任何Docker环境。"""
        invoked.append(arguments)
        raise AssertionError("不应执行服务命令")

    monkeypatch.setitem(script["backup"].__globals__, "command", forbidden_command)
    with pytest.raises(ValueError, match="尚未停止任何服务"):
        script["backup"](environment, "yugitalk-prod")
    assert not invoked


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_s3_preflight_removes_exact_version_and_reports_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_failure: bool
) -> None:
    """探测删除本次上传版本；清理被拒绝时必须报告失败和待清理键。"""
    script = runpy.run_path(str(ROOT / "scripts/check-production-s3.py"))
    values = s3_values() | {
        "STORAGE_ACCESS_KEY": "test-access-key",
        "STORAGE_SECRET_KEY": "test-secret-key-only",
    }
    environment = tmp_path / "production.env"
    environment.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()), encoding="utf-8"
    )
    client = boto3.client(
        "s3",
        region_name="eu-west-1",
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key-only",
    )

    def factory(*arguments: Any, **options: Any) -> Any:
        """使用已准备的离线SDK客户端，禁止凭证链与网络探测。"""
        return client

    def identity() -> str:
        """固定诊断对象标识，便于精确验证请求范围。"""
        return "probe-id"

    monkeypatch.setattr("boto3.client", factory)
    monkeypatch.setitem(script["check_storage"].__globals__, "uuid4", identity)
    key = "yugitalk-milvus/yugitalk-preflight/probe-id"
    content = b"yugitalk-s3-preflight-v1"
    with Stubber(client) as stub:
        for bucket in (values["STORAGE_BUCKET"], values["MILVUS_STORAGE_BUCKET"]):
            stub.add_response("head_bucket", {}, {"Bucket": bucket})
            stub.add_response(
                "list_objects_v2", {}, {"Bucket": bucket, "MaxKeys": 1, "Prefix": key}
            )
            stub.add_response(
                "put_object",
                {"VersionId": "test-version"},
                {"Bucket": bucket, "Key": key, "Body": content},
            )
            stub.add_response(
                "get_object",
                {"Body": StreamingBody(BytesIO(content), len(content))},
                {"Bucket": bucket, "Key": key},
            )
            parameters = {"Bucket": bucket, "Key": key, "VersionId": "test-version"}
            if cleanup_failure:
                stub.add_client_error(
                    "delete_object",
                    service_error_code="AccessDenied",
                    http_status_code=403,
                    expected_params=parameters,
                )
            else:
                stub.add_response("delete_object", {}, parameters)
                stub.add_client_error(
                    "head_object",
                    service_error_code="404",
                    http_status_code=404,
                    expected_params={"Bucket": bucket, "Key": key},
                )
        report = script["check_storage"](environment, True)
        stub.assert_no_pending_responses()
    assert report["status"] == ("failed" if cleanup_failure else "passed")
    assert all(row["cleanup_verified"] is not cleanup_failure for row in report["buckets"])
    if cleanup_failure:
        assert all(row["cleanup_key"] == key for row in report["buckets"])

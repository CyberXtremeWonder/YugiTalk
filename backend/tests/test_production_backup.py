"""验证冷备冻结实际镜像及失败恢复边界，测试不操作真实容器或业务数据。"""

import json
import runpy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.mark.parametrize("stop_failure", [False, True])
def test_backup_freezes_running_image_and_restores_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop_failure: bool
) -> None:
    """发行标签已变化时仍以运行镜像复制与重启；停机失败不可生成成功快照。"""
    root = Path(__file__).resolve().parents[2]
    script = runpy.run_path(str(root / "scripts/production-data.py"))
    namespace = script["backup"].__globals__
    environment = tmp_path / "fixture.env"
    environment.write_text(
        "STORAGE_ENDPOINT=http://storage:9000\nBACKEND_IMAGE=yugitalk-backend:0.2.0\n",
        encoding="utf-8",
    )
    override = tmp_path / "docker-compose.fixture.yml"
    override.write_text("services: {}\n", encoding="utf-8")
    original_backend = "sha256:" + "a" * 64
    original_frontend = "sha256:" + "b" * 64
    copied_images: list[str] = []
    restarts: list[dict[str, str]] = []

    def helpers() -> Any:
        """使用临时数据目录，拒绝接触真实生产目录。"""
        return SimpleNamespace(project_directory=lambda _: tmp_path)

    def command(arguments: list[str], process_env: dict[str, str] | None = None) -> str:
        """模拟运行版本与可变标签不同，并捕获停机失败后的重启版本。"""
        if "ps" in arguments:
            return arguments[-1] + "-container"
        if arguments[:2] == ["docker", "inspect"]:
            container = arguments[2]
            if "--format" in arguments:
                return original_backend if container == "api-container" else original_frontend
            folder = {
                "postgres-container": "postgres",
                "storage-container": "minio",
                "milvus-container": "milvus",
            }[container]
            return json.dumps(
                [
                    {
                        "Config": {"Labels": {"com.docker.compose.project": "yugitalk-test"}},
                        "Mounts": [{"Type": "bind", "Source": str(tmp_path / folder)}],
                    }
                ]
            )
        if "stop" in arguments and stop_failure:
            raise RuntimeError("模拟停机失败")
        if "up" in arguments:
            assert process_env is not None
            restarts.append(process_env)
        return ""

    def copy_directory(source: Path, target: Path, image: str) -> dict[str, str]:
        """记录复制所用镜像，仅在临时目录产生空快照目录。"""
        copied_images.append(image)
        target.mkdir()
        return {"manifest_sha256": "test-hash"}

    monkeypatch.setitem(namespace, "ROOT", tmp_path)
    monkeypatch.setitem(namespace, "production_helpers", helpers)
    monkeypatch.setitem(namespace, "command", command)
    monkeypatch.setitem(namespace, "copy_directory", copy_directory)
    if stop_failure:
        with pytest.raises(RuntimeError, match="模拟停机失败"):
            script["backup"](environment, "yugitalk-test", override)
    else:
        script["backup"](environment, "yugitalk-test", override)
        assert copied_images == [original_backend] * len(script["DATA_FOLDERS"])
    assert len(restarts) == 1
    assert restarts[0]["BACKEND_IMAGE"] == original_backend
    assert restarts[0]["FRONTEND_IMAGE"] == original_frontend
    manifest = json.loads(next((tmp_path / "backups").glob("*/manifest.json")).read_text())
    assert manifest["image"] == original_backend
    assert manifest["runtime_images"]["backend"] == original_backend
    assert (manifest["status"] == "passed") is not stop_failure

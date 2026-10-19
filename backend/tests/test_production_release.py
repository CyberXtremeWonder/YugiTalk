"""发布边界测试：拒绝不兼容迁移，失败时恢复原镜像且保留可审计记录。"""

import json
import runpy
from pathlib import Path

import pytest


@pytest.mark.parametrize("compatible,retained", [(True, True), (False, True), (True, False)])
def test_release_recovers_previous_images_or_rejects_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compatible: bool, retained: bool
) -> None:
    """模拟健康检查失败；代码回滚不应恢复数据库或丢弃失败发布记录。"""
    root = Path(__file__).resolve().parents[2]
    script = runpy.run_path(str(root / "scripts/deploy-production.py"))
    namespace = script["release"].__globals__
    (tmp_path / "backups").mkdir()
    environment_file = tmp_path / "production.env"
    environment_file.write_text("PROD_DATA_ROOT=data/production\n", encoding="utf-8")
    commands: list[tuple[list[str], dict[str, str]]] = []
    backups: list[str] = []

    def backup(*_: object) -> Path:
        """记录备份边界，不运行容器或复制真实数据。"""
        backups.append("created")
        return root / "data/production/backups/test"

    def fake_helpers(path: str) -> dict[str, object]:
        """仅替换生产工具函数，测试发布脚本原有的错误和恢复路径。"""
        if path.endswith("init-production.py"):
            return {"project_directory": lambda _: tmp_path}
        return {"compose_arguments": lambda *_: ["docker", "compose"], "backup": backup}

    def command(arguments: list[str], environment: dict[str, str]) -> str:
        """返回原镜像与数据库版本，仅新镜像的健康检查失败。"""
        commands.append((arguments, environment.copy()))
        if "psql" in arguments:
            return "0007_reliability" if compatible else "incompatible_schema"
        if "ps" in arguments:
            return arguments[-1] + "-container"
        if "image" in arguments:
            if not retained and arguments[3].startswith("sha256:old-"):
                raise RuntimeError("历史镜像已不存在")
            return (
                arguments[3] if arguments[3].startswith("sha256:") else "sha256:new-" + arguments[3]
            )
        if "inspect" in arguments:
            return "sha256:old-" + arguments[2]
        if "up" in arguments and environment["BACKEND_IMAGE"].startswith("sha256:new-"):
            raise RuntimeError("新镜像未通过健康检查")
        return ""

    monkeypatch.setattr(runpy, "run_path", fake_helpers)
    monkeypatch.setitem(namespace, "run", command)
    with pytest.raises(RuntimeError if compatible else ValueError):
        script["release"](
            environment_file, "yugitalk-test", "backend", "frontend", "0007_reliability"
        )
    if compatible and retained:
        deployments = [environment for arguments, environment in commands if "up" in arguments]
        assert len(deployments) == 2
        assert deployments[-1]["BACKEND_IMAGE"] == "sha256:old-api-container"
        assert deployments[-1]["FRONTEND_IMAGE"] == "sha256:old-frontend-container"
        record = json.loads((tmp_path / "backups/last-release.json").read_text(encoding="utf-8"))
        assert record["status"] == "failed_reverted"
        assert backups == ["created"]
    else:
        assert not backups and not any("up" in arguments for arguments, _ in commands)

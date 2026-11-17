"""验证镜像重建的保留顺序，所有Docker命令均为本地替身。"""

import runpy
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("state", ["existing", "missing", "inspect_failed", "retain_failed"])
@pytest.mark.parametrize(
    "component,version",
    [("backend", "0.2.0"), ("frontend", "0.2.0"), ("embedding", "0.2.0"), ("docling", "0.1.0")],
)
def test_build_preserves_previous_before_rebinding_tag(
    state: str, component: str, version: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧镜像必须先保留；未知状态或保留失败不得继续构建。"""
    script = runpy.run_path(str(ROOT / "scripts/build-production-images.py"))
    calls = []
    identity = "sha256:" + "a" * 64

    def command(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        """模拟Docker结果并记录顺序，禁止操作真实镜像。"""
        calls.append(arguments)
        if arguments[1:3] == ["image", "inspect"]:
            if state == "missing":
                return subprocess.CompletedProcess(arguments, 1, "", "No such image")
            if state == "inspect_failed":
                return subprocess.CompletedProcess(arguments, 1, "", "daemon unavailable")
            return subprocess.CompletedProcess(arguments, 0, identity, "")
        return subprocess.CompletedProcess(arguments, 1 if state == "retain_failed" else 0, "", "")

    monkeypatch.setitem(script["build_component"].__globals__, "command", command)
    if state in {"inspect_failed", "retain_failed"}:
        with pytest.raises(RuntimeError):
            script["build_component"](component)
        assert not any(arguments[1] == "build" for arguments in calls)
    else:
        script["build_component"](component)
        target = f"yugitalk-{component}:{version}"
        assert calls[0] == ["docker", "image", "inspect", target, "--format", "{{.Id}}"]
        context = (
            ["-f", str(ROOT / "embedding/Dockerfile"), str(ROOT)]
            if component == "embedding"
            else [str(ROOT / component)]
        )
        assert calls[-1] == ["docker", "build", "-t", target, *context]
        if state == "existing":
            assert calls[1] == [
                "docker",
                "image",
                "tag",
                identity,
                f"yugitalk-{component}:build-previous",
            ]
        else:
            assert len(calls) == 2


@pytest.mark.parametrize(
    "components",
    [[], ["docling"], ["backend", "frontend", "docling"]],
)
def test_build_selection_includes_docling(
    components: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认构建全部生产服务，也允许远端嵌入部署显式选择Docling。"""
    script = runpy.run_path(str(ROOT / "scripts/build-production-images.py"))
    built = []
    monkeypatch.setitem(script["main"].__globals__, "build_component", built.append)
    arguments = ["build-production-images.py"]
    for component in components:
        arguments.extend(["--component", component])
    monkeypatch.setattr("sys.argv", arguments)
    script["main"]()
    assert built == (components or ["backend", "frontend", "embedding", "docling"])

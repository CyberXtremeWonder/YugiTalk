"""构建本项目生产镜像，先保留上一镜像，避免重建标签使容器引用失效。"""

import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ("backend", "frontend", "embedding")


def command(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    """捕获有界Docker操作输出，不读取或打印私有配置。"""
    return subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=1800,
        check=False,
    )


def build_component(component: str) -> None:
    """先保留实际旧ID到单个previous标签，再构建新标签；不操作运行容器。"""
    if component not in COMPONENTS:
        raise ValueError("只允许本项目的三类镜像")
    repository = "yugitalk-" + component
    target = repository + ":0.2.0"
    inspected = command(["docker", "image", "inspect", target, "--format", "{{.Id}}"])
    if inspected.returncode == 0:
        identity = inspected.stdout.strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", identity):
            raise RuntimeError("镜像身份无效，停止构建")
        retained = command(
            ["docker", "image", "tag", identity, repository + ":build-previous"]
        )
        if retained.returncode:
            raise RuntimeError("无法保留上一镜像，停止构建")
    elif "No such image" not in inspected.stderr:
        raise RuntimeError("无法确认旧镜像状态，停止构建")
    arguments = ["docker", "build", "-t", target]
    if component == "embedding":
        arguments += ["-f", str(ROOT / "embedding/Dockerfile"), str(ROOT)]
    else:
        arguments += [str(ROOT / component)]
    result = command(arguments)
    if result.returncode:
        raise RuntimeError("镜像构建失败；上一镜像保留在build-previous")
    suffix = (
        "上一镜像标签已保留。"
        if inspected.returncode == 0
        else "首次构建，无上一镜像。"
    )
    print(component + "镜像已构建；" + suffix, flush=True)


def main() -> None:
    """按选择构建，默认三类镜像；变更期间不得并行发布或重新构建同一标签。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--component", action="append", choices=COMPONENTS)
    args = parser.parse_args()
    for component in args.component or COMPONENTS:
        build_component(component)


if __name__ == "__main__":
    main()

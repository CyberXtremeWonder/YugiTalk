"""单机生产冷备与独立目录恢复，跨存储必须同时停止写入再复制。"""

import argparse
import importlib.util
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DATA_FOLDERS = (
    "postgres",
    "redis",
    "minio",
    "etcd",
    "milvus",
    "config",
    "tls",
    "prometheus",
    "jaeger",
    "s3-ca",
)
WRITERS = [
    "frontend",
    "api",
    "scheduler",
    "worker",
    "milvus",
    "postgres",
    "redis",
    "etcd",
    "storage",
    "prometheus",
    "jaeger",
]


def command(arguments: list[str], environment: dict[str, str] | None = None) -> str:
    """执行有界命令并捕获输出，不打印 Compose 解析后的凭证。"""
    result = subprocess.run(
        arguments,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=1200,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr[-3000:] or "生产操作失败")
    return result.stdout


def production_helpers() -> object:
    """按文件路径加载共享的生产目录边界检查，脚本名不影响复用。"""
    spec = importlib.util.spec_from_file_location(
        "production_init", ROOT / "scripts/init-production.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def copy_directory(source: Path, target: Path, image: str) -> dict[str, object]:
    """离线复制只读源目录，复用逐文件散列校验，禁止覆盖目标。"""
    target.mkdir(parents=True, exist_ok=False)
    return json.loads(
        command(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--user",
                "0",
                "--entrypoint",
                "python",
                "--mount",
                f"type=bind,source={source},target=/source,readonly",
                "--mount",
                f"type=bind,source={target},target=/target",
                "--mount",
                f"type=bind,source={ROOT / 'scripts/copy-persistent-data.py'},target=/copy.py,readonly",
                image,
                "/copy.py",
                "/source",
                "/target",
            ]
        )
    )


def compose_arguments(
    env_file: Path, project: str, override: Path | None = None
) -> list[str]:
    """明确合并生产文件并固定独立项目名，禁止操作开发 Compose 项目。"""
    if not project.startswith("yugitalk-") or project in {
        "yugitalk-dev",
        "yugitalk-stage0",
    }:
        raise ValueError("生产操作必须使用独立的 yugitalk 项目名")
    arguments = [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "-f",
        str(ROOT / "docker-compose.yml"),
        "-f",
        str(ROOT / "docker-compose.prod.yml"),
        "-p",
        project,
    ]
    if override:
        override = override.resolve(strict=True)
        if not override.is_relative_to(ROOT):
            raise ValueError("附加部署配置必须属于项目目录")
        arguments.extend(["-f", str(override)])
    return arguments


def backup(env_file: Path, project: str, override: Path | None = None) -> Path:
    """停止所有跨存储写入者完成冷备，成功或失败都恢复原生产服务。"""
    from dotenv import dotenv_values

    values = dotenv_values(env_file, interpolate=False)
    if (
        urlsplit(values.get("STORAGE_ENDPOINT") or "").hostname != "storage"
        or override is None
    ):
        raise ValueError(
            "此脚本仅备份本机对象存储；现有S3需完成对象版本/快照备份与恢复演练后再发布，尚未停止任何服务"
        )
    helpers = production_helpers()
    data = helpers.project_directory(values.get("PROD_DATA_ROOT") or "data/production")
    compose = compose_arguments(env_file, project, override)
    restart_environment = dict(os.environ)
    for service, key in (("api", "BACKEND_IMAGE"), ("frontend", "FRONTEND_IMAGE")):
        container = command([*compose, "ps", "-q", service]).strip()
        restart_environment[key] = command(
            ["docker", "inspect", container, "--format", "{{.Image}}"]
        ).strip()
        # 未保留的历史镜像可能只剩运行容器快照；停机前必须能重新创建服务。
        command(["docker", "image", "inspect", restart_environment[key]])
        component = "backend" if service == "api" else "frontend"
        command(
            [
                "docker",
                "image",
                "tag",
                restart_environment[key],
                "yugitalk-"
                + component
                + ":rollback-"
                + restart_environment[key].split(":")[-1][:12],
            ]
        )
    # 复制和后续恢复也固定停机前版本，不依赖可能被重新绑定的发行标签。
    image = restart_environment["BACKEND_IMAGE"]
    # 当前服务必须确实挂载预期目录，防止对错误环境进行备份。
    for service, directory in (
        ("postgres", "postgres"),
        ("storage", "minio"),
        ("milvus", "milvus"),
    ):
        container = command([*compose, "ps", "-q", service]).strip()
        state = json.loads(command(["docker", "inspect", container]))[0]
        assert state["Config"]["Labels"]["com.docker.compose.project"] == project
        assert any(
            row["Type"] == "bind"
            and Path(row["Source"]).resolve() == (data / directory).resolve()
            for row in state["Mounts"]
        )
    destination = data / "backups" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination.mkdir(parents=True, exist_ok=False)
    report = {
        "storage_scope": "local_fixture_only",
        "external_s3_backup_verified": False,
        "created_at": datetime.now(UTC).isoformat(),
        "project": project,
        "image": image,
        "runtime_images": {
            "backend": restart_environment["BACKEND_IMAGE"],
            "frontend": restart_environment["FRONTEND_IMAGE"],
        },
        "copies": {},
        "status": "in_progress",
    }
    try:
        command([*compose, "stop", "frontend", "api", "scheduler", "worker"])
        command([*compose, "stop", "milvus", "prometheus", "jaeger"])
        command([*compose, "stop", "postgres", "redis", "etcd", "storage"])
        for folder in DATA_FOLDERS:
            report["copies"][folder] = copy_directory(
                data / folder, destination / folder, image
            )
        report["status"] = "passed"
    finally:
        (destination / "manifest.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # 恢复停机前的实际镜像 ID，不允许备份操作顺带切换已被重建的可变标签。
        command(
            [*compose, "up", "-d", "--wait", "--wait-timeout", "300"],
            restart_environment,
        )
    return destination


def restore(snapshot: Path, relative: str) -> Path:
    """只向新的项目数据目录恢复完成的快照，不覆盖运行中的数据。"""
    helpers = production_helpers()
    snapshot = snapshot.resolve(strict=True)
    if not snapshot.is_relative_to(ROOT / "data"):
        raise ValueError("恢复快照必须位于本项目 data 目录")
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    if manifest["status"] != "passed":
        raise ValueError("快照未完整通过，不允许恢复")
    destination = helpers.project_directory(relative)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("恢复目标必须为空")
    # 检查全部容器，包括已停止的实例，拒绝恢复到被挂载的目录。
    ids = command(["docker", "ps", "-aq"]).split()
    if ids:
        for container in json.loads(command(["docker", "inspect", *ids])):
            for mount in container["Mounts"]:
                if mount["Type"] == "bind" and Path(
                    mount["Source"]
                ).resolve().is_relative_to(destination.resolve()):
                    raise ValueError("恢复目标仍被容器挂载")
    destination.mkdir(parents=True, exist_ok=True)
    for folder in DATA_FOLDERS:
        if folder == "s3-ca" and folder not in manifest["copies"]:
            # 历史本机快照没有私有S3 CA；恢复为空目录，不把它当作已有证书验证。
            (destination / folder).mkdir()
            continue
        result = copy_directory(
            snapshot / folder, destination / folder, manifest["image"]
        )
        if result["manifest_sha256"] != manifest["copies"][folder]["manifest_sha256"]:
            raise ValueError("恢复文件与快照散列不一致，禁止启动")
    (destination / "backups").mkdir()
    return destination


def main() -> None:
    """提供明确的冷备与新目录恢复命令，恢复后仍需配置和业务验收。"""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    backup_parser = sub.add_parser("backup")
    backup_parser.add_argument("--env-file", type=Path, required=True)
    backup_parser.add_argument("--project", default="yugitalk-prod")
    backup_parser.add_argument("--compose-override", type=Path)
    restore_parser = sub.add_parser("restore")
    restore_parser.add_argument("--snapshot", type=Path, required=True)
    restore_parser.add_argument("--target", required=True)
    args = parser.parse_args()
    result = (
        backup(args.env_file, args.project, args.compose_override)
        if args.action == "backup"
        else restore(args.snapshot, args.target)
    )
    print("操作完成：" + str(result))


if __name__ == "__main__":
    main()

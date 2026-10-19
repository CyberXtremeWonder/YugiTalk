"""将开发环境的持久卷离线迁移到项目目录，保留旧卷并在失败时恢复配置。"""

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "-f", str(ROOT / "docker-compose.yml")]
SERVICES = {
    "postgres": ("postgres-data", "postgres", "/var/lib/postgresql/data"),
    "redis": ("redis-data", "redis", "/data"),
    "storage": ("storage-data", "minio", "/data"),
    "etcd": ("etcd-data", "etcd", "/etcd"),
    "milvus": ("milvus-data", "milvus", "/var/lib/milvus"),
}
CLIENTS = ["frontend", "api", "scheduler", "worker"]


def run(arguments: list[str], input_text: str | None = None) -> str:
    """以参数数组执行命令，保留诊断但不输出环境变量或真实密钥。"""
    result = subprocess.run(
        arguments,
        input=input_text,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=900,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr[-4000:] or "命令执行失败")
    return result.stdout


def probe() -> dict[str, object]:
    """通过现有 API 配置读取迁移前后的业务数据指纹。"""
    return json.loads(
        run(
            [*COMPOSE, "exec", "-T", "api", "python", "-"],
            (ROOT / "scripts/probe-persistent-data.py").read_text(encoding="utf-8"),
        )
    )


def checked_target(relative: str) -> Path:
    """确保空目标位于项目内，拒绝符号链接或目录重解析点。"""
    target = ROOT / "data" / relative
    for parent in [target, *target.parents]:
        if parent == ROOT.parent:
            break
        if parent.is_symlink() or (
            hasattr(parent, "is_junction") and parent.is_junction()
        ):
            raise RuntimeError("目标包含重解析路径")
    if not target.resolve().is_relative_to(ROOT / "data"):
        raise RuntimeError("目标超出项目数据目录")
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        raise RuntimeError(f"目标目录必须为空：{target}")
    return target


def copy_volume(volume: str, target: Path) -> dict[str, object]:
    """使用已安装的后端镜像从只读卷复制，不下载工具或修改原卷。"""
    return json.loads(
        run(
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
                f"type=volume,source={volume},target=/source,readonly",
                "--mount",
                f"type=bind,source={target},target=/target",
                "--mount",
                f"type=bind,source={ROOT / 'scripts/copy-persistent-data.py'},target=/copy.py,readonly",
                "yugitalk-backend:0.1.0",
                "/copy.py",
                "/source",
                "/target",
            ]
        )
    )


def bind_mount(source: str, target: str) -> str:
    """生成跨 Windows 与 Linux 的相对路径挂载，禁止自动创建错误数据目录。"""
    return (
        "volumes:\n      - type: bind\n"
        f"        source: ./{source}\n        target: {target}\n"
        "        bind:\n          create_host_path: false"
    )


def main() -> None:
    """停机复制、修改挂载、验证恢复；任何失败自动还原旧 Compose 配置。"""
    compose_path = ROOT / "docker-compose.yml"
    stage0_path = ROOT / "deploy/archive/stages/docker-compose.stage0.yml"
    original = compose_path.read_text(encoding="utf-8")
    original_stage0 = stage0_path.read_text(encoding="utf-8")
    targets = {}
    for service, (volume, relative, destination) in SERVICES.items():
        data = json.loads(run(["docker", "inspect", f"yugitalk-dev-{service}-1"]))[0]
        labels = data["Config"]["Labels"]
        assert labels["com.docker.compose.project"] == "yugitalk-dev"
        assert labels["com.docker.compose.service"] == service
        mount = next(row for row in data["Mounts"] if row["Destination"] == destination)
        assert mount["Type"] == "volume" and mount["Name"] == f"yugitalk-dev_{volume}"
        # 不允许与运行中的其他容器共享可写源卷。
        consumers = run(
            [
                "docker",
                "ps",
                "--filter",
                f"volume={mount['Name']}",
                "--format",
                "{{.Names}}",
            ]
        )
        assert consumers.strip() == f"yugitalk-dev-{service}-1"
        targets[service] = checked_target(relative)
    stage0_running = run(
        [
            "docker",
            "ps",
            "--filter",
            "label=com.docker.compose.project=yugitalk-stage0",
            "-q",
        ]
    )
    assert not stage0_running.strip(), "阶段 0 仍在运行，停止迁移"
    stage0_target = checked_target("stage0/postgres")
    assert shutil.disk_usage(ROOT).free > 20 * 1024**3, "项目磁盘剩余空间不足"
    active = run(
        [
            *COMPOSE,
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "yugitalk",
            "-d",
            "yugitalk",
            "-At",
            "-c",
            "SELECT (SELECT count(*) FROM runs WHERE status IN ('queued','running')) + (SELECT count(*) FROM documents WHERE status IN ('queued','parsing','indexing')); ",
        ]
    )
    assert active.strip() == "0", "有执行中的任务，停止迁移"
    before = probe()
    backup = (
        ROOT
        / ".cache"
        / ("data-migration-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S"))
    )
    backup.mkdir(parents=True)
    (backup / "docker-compose.before.yml").write_text(original, encoding="utf-8")
    (backup / "docker-compose.stage0.before.yml").write_text(
        original_stage0, encoding="utf-8"
    )
    report = {
        "status": "in_progress",
        "before": before,
        "copies": {},
        "backup_directory": str(backup),
    }
    report_path = ROOT / "docs/stages/artifacts/project-data-migration.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    try:
        print("停止应用服务与数据服务，开始离线迁移。", flush=True)
        run([*COMPOSE, "stop", *CLIENTS])
        run([*COMPOSE, "stop", "milvus"])
        run([*COMPOSE, "stop", "postgres", "redis", "etcd", "storage"])
        for service, (volume, relative, destination) in SERVICES.items():
            report["copies"][service] = copy_volume(
                f"yugitalk-dev_{volume}", targets[service]
            )
            print(f"{service}：复制与 SHA-256 校验通过。", flush=True)
        existing_volumes = run(["docker", "volume", "ls", "--format", "{{.Name}}"])
        if "yugitalk-stage0_probe-db" in existing_volumes.splitlines():
            report["copies"]["stage0_postgres"] = copy_volume(
                "yugitalk-stage0_probe-db", stage0_target
            )
        updated = original
        for volume, relative, destination in SERVICES.values():
            previous = f'volumes: ["{volume}:{destination}"]'
            assert updated.count(previous) == 1
            updated = updated.replace(
                previous, bind_mount("data/" + relative, destination)
            )
        updated = updated[: updated.rindex("\nvolumes:\n")] + "\n"
        compose_path.write_text(updated, encoding="utf-8")
        updated_stage0 = original_stage0.replace(
            'volumes: ["probe-db:/var/lib/postgresql/data"]',
            bind_mount("data/stage0/postgres", "/var/lib/postgresql/data"),
        ).replace(
            "  setup:\n",
            "    " + bind_mount("data/stage0/redis", "/data") + "\n  setup:\n",
        )
        updated_stage0 = updated_stage0[: updated_stage0.rindex("\nvolumes:\n")] + "\n"
        stage0_path.write_text(updated_stage0, encoding="utf-8")
        run([*COMPOSE, "config", "--quiet"])
        run(["docker", "compose", "--project-directory", str(ROOT), "-f", str(stage0_path), "config", "--quiet"])
        print("启动新挂载服务并等待健康检查。", flush=True)
        run([*COMPOSE, "up", "-d", "--wait", "--wait-timeout", "240"])
        after = probe()
        assert before == after, "迁移后的业务指纹不一致"
        report.update(status="passed", after=after, old_volumes_retained=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("所有持久服务已迁移，业务数据指纹一致，旧卷保留。", flush=True)
    except Exception:
        # 停止新实例后再启用原卷，防止两套实例同时处理任务。
        run(
            [
                *COMPOSE,
                "stop",
                *CLIENTS,
                "milvus",
                "postgres",
                "redis",
                "etcd",
                "storage",
            ]
        )
        compose_path.write_text(original, encoding="utf-8")
        stage0_path.write_text(original_stage0, encoding="utf-8")
        run([*COMPOSE, "up", "-d", "--wait", "--wait-timeout", "240"])
        report.update(status="failed_rolled_back", old_volumes_retained=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        raise


if __name__ == "__main__":
    main()

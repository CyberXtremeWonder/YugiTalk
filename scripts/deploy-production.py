"""在兼容数据库版本内发布固定镜像，健康检查失败时恢复上一组镜像。"""

import argparse
import json
import os
import runpy
import subprocess
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def run(arguments: list[str], environment: dict[str, str]) -> str:
    """运行有界发布命令，不打印配置和凭证，失败仅给出命令类别。"""
    result = subprocess.run(
        arguments,
        env=environment,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=600,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("发布命令失败：" + arguments[1])
    return result.stdout


def image_id(image: str, environment: dict[str, str]) -> str:
    """要求镜像已在本机，经 ID 固定本次发布，避免可变标签影响回滚。"""
    return run(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"], environment
    ).strip()


def release(
    env_file: Path,
    project: str,
    backend: str,
    frontend: str,
    schema: str,
    override: Path | None = None,
    rollback: bool = False,
) -> Path:
    """发布前冷备并核对迁移版本，只回退代码镜像，不自动降级数据库。"""
    helpers = runpy.run_path(str(ROOT / "scripts/production-data.py"))
    initializer = runpy.run_path(str(ROOT / "scripts/init-production.py"))
    values = dotenv_values(env_file, interpolate=False)
    data = initializer["project_directory"](
        values.get("PROD_DATA_ROOT") or "data/production"
    )
    arguments = helpers["compose_arguments"](env_file, project, override)
    environment = dict(os.environ)
    # 操作系统残留的镜像变量不能改变明确环境文件的部署状态。
    environment.pop("BACKEND_IMAGE", None)
    environment.pop("FRONTEND_IMAGE", None)
    revision = run(
        [
            *arguments,
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
            "SELECT version_num FROM alembic_version;",
        ],
        environment,
    ).strip()
    if revision != schema:
        raise ValueError("数据库版本不符合声明的兼容范围；禁止自动回滚迁移")
    previous = {}
    for service, key in (("api", "BACKEND_IMAGE"), ("frontend", "FRONTEND_IMAGE")):
        container = run([*arguments, "ps", "-q", service], environment).strip()
        previous[key] = run(
            ["docker", "inspect", container, "--format", "{{.Image}}"], environment
        ).strip()
        # 容器仍运行不代表旧镜像还可被重新创建，必须在停机前验证并打保留标签。
        image_id(previous[key], environment)
        component = "backend" if service == "api" else "frontend"
        run(
            [
                "docker",
                "image",
                "tag",
                previous[key],
                "yugitalk-"
                + component
                + ":rollback-"
                + previous[key].split(":")[-1][:12],
            ],
            environment,
        )
    state_file = data / "backups/last-release.json"
    if rollback:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        if state["schema"] != revision or state["project"] != project:
            raise ValueError("回滚记录不属于当前环境或数据库版本")
        target = state["previous"]
        for image in target.values():
            image_id(image, environment)
    else:
        target = {
            "BACKEND_IMAGE": image_id(backend, environment),
            "FRONTEND_IMAGE": image_id(frontend, environment),
        }
    snapshot = helpers["backup"](env_file, project, override)
    state = {
        "project": project,
        "schema": revision,
        "previous": previous,
        "target": target,
        "snapshot": str(snapshot.relative_to(ROOT)),
        "status": "deploying",
    }
    try:
        run(
            [*arguments, "up", "-d", "--wait", "--wait-timeout", "180"],
            {**environment, **target},
        )
        state["status"] = "rolled_back" if rollback else "passed"
    except Exception:
        run(
            [*arguments, "up", "-d", "--wait", "--wait-timeout", "180"],
            {**environment, **previous},
        )
        state["status"] = "failed_reverted"
        raise
    finally:
        state_file.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return state_file


def main() -> None:
    """接受明确的镜像和兼容迁移版本，生产文件与测试覆盖文件分开指定。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--project", default="yugitalk-prod")
    parser.add_argument("--backend-image", default="yugitalk-backend:0.2.0")
    parser.add_argument("--frontend-image", default="yugitalk-frontend:0.2.0")
    parser.add_argument("--compatible-schema", required=True)
    parser.add_argument("--compose-override", type=Path)
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    release(
        args.env_file,
        args.project,
        args.backend_image,
        args.frontend_image,
        args.compatible_schema,
        args.compose_override,
        args.rollback,
    )
    print("生产镜像操作与健康检查通过；数据库未执行降级。")


if __name__ == "__main__":
    main()

"""首次部署入口：校验、初始化、启动和HTTPS检查；不执行备份或恢复演练。"""

import argparse
import json
import os
import re
import runpy
import ssl
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
PERSISTENT = ("postgres", "redis", "etcd", "milvus", "prometheus", "jaeger")


def command(arguments: list[str], environment: dict[str, str]) -> str:
    """捕获有界命令输出，错误仅给出类别，避免Compose回显私有配置。"""
    result = subprocess.run(
        arguments,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=900,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("Docker命令失败，请在服务器检查相应服务状态与脱敏日志")
    return result.stdout


def clean_environment(values: dict[str, str | None]) -> dict[str, str]:
    """清除环境文件同名覆盖及隐式Compose选择，固定使用本次声明的部署配置。"""
    environment = dict(os.environ)
    for key in values:
        environment.pop(key, None)
    for key in (
        "COMPOSE_FILE",
        "COMPOSE_PROJECT_NAME",
        "COMPOSE_PROFILES",
        "COMPOSE_ENV_FILES",
        "COMPOSE_DISABLE_ENV_FILE",
        "BACKEND_IMAGE",
        "FRONTEND_IMAGE",
    ):
        environment.pop(key, None)
    return environment


def check_certificate(root: Path, origin: str) -> None:
    """确认正式证书与私钥可用；公网信任、域名和有效期由部署后的HTTPS请求核验。"""
    host = urlsplit(origin).hostname or ""
    if host in {"localhost", "127.0.0.1", "agent.example.com"} or host.endswith(
        (".example", ".test", ".invalid")
    ):
        raise ValueError("必须使用实际部署域名，不能使用示例或隔离验收地址")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(root / "tls/fullchain.pem", root / "tls/privkey.pem")


def check_empty_target(root: Path) -> None:
    """首次部署只能使用没有业务数据的目录，不覆盖开发、既有生产或失败启动的数据。"""
    for name in PERSISTENT:
        directory = root / name
        if directory.is_symlink() or directory.is_junction():
            raise ValueError("持久目录不允许重解析路径")
        if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
            raise ValueError("目标已有持久数据；首次部署入口不用于升级或重试已写入数据的部署")


def prepare(env_file: Path, project: str) -> dict[str, Any]:
    """只读预检私有配置、空目标、证书、Compose和镜像；不访问S3或调用模型。"""
    env_file = env_file.resolve(strict=True)
    if not env_file.is_relative_to(ROOT) or env_file.name.endswith(".example"):
        raise ValueError("必须使用项目内实际填写的私有环境文件")
    if not re.fullmatch(r"yugitalk-[a-z0-9][a-z0-9-]{0,50}", project) or project in {
        "yugitalk-dev",
        "yugitalk-stage0",
        "yugitalk-stage6",
    }:
        raise ValueError("必须使用独立的生产项目名")
    values = dict(dotenv_values(env_file, interpolate=False))
    initializer = runpy.run_path(str(ROOT / "scripts/init-production.py"))
    root, _ = initializer["validate_values"](values)
    check_empty_target(root)
    check_certificate(root, values.get("PUBLIC_ORIGIN") or "")
    environment = clean_environment(values)
    profiles = (values.get("COMPOSE_PROFILES") or "").split(",")
    if any(profile not in {"", "local-embedding"} for profile in profiles):
        raise ValueError("首次部署只允许local-embedding配置，禁止启用本机测试存储")
    compose = [
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
    # 生产JSON只留在当前进程，不打印或写入报告，其中可能包含凭证。
    merged = json.loads(command([*compose, "config", "--format", "json"], environment))
    if "local-embedding" in profiles:
        mounts = merged["services"]["embedding"].get("volumes", [])
        source = next(
            (mount.get("source") for mount in mounts if mount.get("target") == "/models"), None
        )
        if not source or not Path(source).is_dir():
            raise ValueError("本地BGE-M3缓存目录不存在，请填写服务器自身的模型路径")
    elif (
        urlsplit(values.get("EMBEDDING_BASE_URL") or "http://embedding:8000/v1").hostname
        == "embedding"
    ):
        raise ValueError("未启用本地嵌入服务时必须填写可访问的远端嵌入服务地址")
    from sys import path

    if str(ROOT / "backend") not in path:
        path.insert(0, str(ROOT / "backend"))
    from app.core.config import Settings

    api_values = merged["services"]["api"]["environment"]
    supplied = {}
    for field in Settings.model_fields:
        key = field.upper()
        if key in api_values:
            value = api_values[key]
            if field in {
                "auth_trusted_origins",
                "cors_origins",
                "tool_http_allowed_hosts",
                "tool_secrets",
            }:
                value = json.loads(value)
            supplied[field] = value
    # 私有CA尚未复制到容器挂载路径，其源文件由共享初始化校验器核对。
    if supplied.get("storage_ca_file"):
        supplied["storage_ca_file"] = str((ROOT / str(values["S3_CA_SOURCE"])).resolve())
    # BaseSettings的运行时来源参数不属于Pydantic生成的字段签名，统一通过字典传入。
    Settings(**{"_env_file": None, "_env_prefix": "YUGITALK_FIRST_DEPLOY_UNUSED_", **supplied})
    if command([*compose, "ps", "--all", "--quiet"], environment).strip():
        raise ValueError("目标Compose项目已有容器，不能作为首次部署启动")
    images = {}
    for name, service in merged["services"].items():
        required_profiles = service.get("profiles", [])
        if required_profiles and not set(required_profiles).intersection(profiles):
            continue
        identity = command(
            ["docker", "image", "inspect", service["image"], "--format", "{{.Id}}"], environment
        ).strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", identity):
            raise ValueError("镜像身份无效，请先构建应用镜像并拉取基础服务镜像")
        images[name] = identity
    return {
        "root": root,
        "values": values,
        "environment": environment,
        "compose": compose,
        "images": images,
        "env_file": env_file,
        "initializer": initializer,
    }


def verify_https(origin: str) -> None:
    """通过实际域名验证系统信任链、HTTPS页面和依赖就绪，不关闭证书校验。"""
    with httpx.Client(trust_env=False, timeout=15, follow_redirects=False) as client:
        home = client.get(origin + "/")
        ready = client.get(origin + "/health/ready")
        if home.status_code != 200 or ready.status_code != 200:
            raise RuntimeError("公网HTTPS或依赖就绪检查未通过")
        checks = ready.json().get("checks", {})
        if not checks or not all(checks.values()):
            raise RuntimeError("项目依赖未全部就绪")


def deploy(plan: dict[str, Any], project: str) -> dict[str, Any]:
    """首次启动不执行备份或恢复演练；失败保留现场，禁止删除数据或声称已回滚。"""
    values = plan["values"]
    storage = runpy.run_path(str(ROOT / "scripts/check-production-s3.py"))
    probe = storage["check_storage"](plan["env_file"], write_probe=True)
    if probe["status"] != "passed":
        raise RuntimeError("S3两桶读写删除检查失败，尚未启动任何项目服务")
    root = plan["initializer"]["initialize"](plan["env_file"])
    record = {
        "project": project,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "starting",
        "images": plan["images"],
        "s3_verified": True,
        "https_verified": False,
        "restore_drill": "not_performed",
        "backup_created": False,
        "model_quality_verified": False,
    }
    report = root / "first-deployment.json"
    pins = root / "config/first-images.compose.json"
    plan["initializer"]["write_private"](
        pins,
        json.dumps(
            {
                "services": {
                    service: {"image": identity} for service, identity in plan["images"].items()
                }
            },
            indent=2,
        )
        + "\n",
    )
    environment = {
        **plan["environment"],
        "BACKEND_IMAGE": plan["images"]["api"],
        "FRONTEND_IMAGE": plan["images"]["frontend"],
    }
    try:
        plan["initializer"]["configure_permissions"](root, plan["images"]["api"])
        command(
            [
                *plan["compose"],
                "-f",
                str(pins),
                "up",
                "-d",
                "--no-build",
                "--pull",
                "never",
                "--wait",
                "--wait-timeout",
                "600",
            ],
            environment,
        )
        verify_https(values["PUBLIC_ORIGIN"])
        record.update(status="deployed", https_verified=True)
    except Exception:
        record["status"] = "failed"
        raise
    finally:
        report.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return record


def main() -> int:
    """提供无写入预检及正式首次启动入口，失败不输出私有配置或异常原文。"""
    parser = argparse.ArgumentParser(description="YugiTalk首次生产部署，不执行恢复演练")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--project", default="yugitalk-prod")
    parser.add_argument("--check", action="store_true", help="只读预检，不创建目录或启动服务")
    args = parser.parse_args()
    try:
        plan = prepare(args.env_file, args.project)
        if args.check:
            print("首次部署预检通过；尚未检查真实S3读写和公网HTTPS。")
        else:
            deploy(plan, args.project)
            print("首次部署与HTTPS健康检查通过；恢复演练未执行，模型质量尚未验证。")
        return 0
    except Exception as error:
        print(f"首次部署失败：{type(error).__name__}；请检查配置、证书、镜像及目标是否为空。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

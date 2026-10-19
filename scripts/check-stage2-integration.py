"""隔离数据库的真实迁移与容器浏览器验收；仅清理本次创建的测试资源。"""

import json
import os
import re
import secrets
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
from redis import Redis

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings
from app.identity.login_limit import RedisLoginLimiter
from app.identity.security import digest


def run(
    args: list[str],
    env: dict[str, str],
    cwd: Path = ROOT,
    stdin: str | None = None,
    expected: int = 0,
) -> None:
    """不打印环境变量或标准输入，避免将随机测试凭证写进产物。"""
    result = subprocess.run(args, cwd=cwd, env=env, input=stdin, text=True, check=False)
    if result.returncode != expected:
        raise RuntimeError(f"验收命令失败：{args[0]}，退出码 {result.returncode}")


def main() -> None:
    """运行本文件定义的验收步骤，并保存可复核的结果。"""
    stage = (
        5
        if "--stage5" in sys.argv
        else 4
        if "--stage4" in sys.argv
        else 3
        if "--stage3" in sys.argv
        else 2
    )
    settings = Settings(app_env="test")
    base_url = settings.database_url.get_secret_value()
    database = f"yugitalk_stage{stage}_" + uuid.uuid4().hex
    if not re.fullmatch(r"yugitalk_stage[2345]_[0-9a-f]{32}", database):
        raise RuntimeError("临时数据库名称无效")
    parts = urlsplit(base_url)
    test_url = urlunsplit(parts._replace(path="/" + database))
    password = secrets.token_urlsafe(24)
    secret = secrets.token_urlsafe(36)
    env = {
        **os.environ,
        "APP_ENV": "test",
        "DATABASE_URL": test_url,
        "SESSION_SECRET": secret,
        "E2E_PASSWORD": password,
        "STAGE2_SESSION_SECRET": secret,
        "STAGE2_DATABASE_URL": test_url.replace(
            "127.0.0.1", "host.docker.internal"
        ).replace("localhost", "host.docker.internal"),
        "PYTHONUTF8": "1",
        "STAGE4_STORAGE_BUCKET": "yugitalk-stage4-" + uuid.uuid4().hex,
        "STAGE4_COLLECTION_PREFIX": "yugitalk_s4_" + uuid.uuid4().hex[:16],
        "STAGE4_EMBEDDING_API_KEY": settings.embedding_api_key.get_secret_value(),
        "STAGE4_EMBEDDING_BASE_URL": settings.embedding_base_url.replace(
            "127.0.0.1", "host.docker.internal"
        ).replace("localhost", "host.docker.internal"),
        "STAGE3_TASK_QUEUE": "yugitalk-test-" + uuid.uuid4().hex,
    }
    compose = [
        "docker",
        "compose",
        "--project-directory",
        str(ROOT),
        "-f",
        "deploy/archive/stages/docker-compose.stage2-test.yml",
    ]
    if stage >= 3:
        compose += ["-f", "deploy/archive/stages/docker-compose.stage3-test.yml"]
    if stage >= 4:
        compose += ["-f", "deploy/archive/stages/docker-compose.stage4-test.yml"]
    if stage == 5:
        compose += ["-f", "deploy/archive/stages/docker-compose.stage5-test.yml"]
    compose += ["-p", f"yugitalk-stage{stage}-test"]
    report = {"database_isolated": True, "checks": [], "cleanup": False}
    created = False
    with psycopg.connect(base_url, autocommit=True) as admin:
        try:
            admin.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database))
            )
            created = True
            for args in (
                ["upgrade", "head"],
                ["downgrade", "0003_identity" if stage >= 3 else "0002_milvus"],
                ["upgrade", "head"],
            ):
                run([sys.executable, "-m", "alembic", *args], env, ROOT / "backend")
            report["checks"].append("真实 PostgreSQL：新库迁移、身份迁移回滚、再次升级")
            for email, name, space, platform in (
                ("owner@example.test", "验收所有者", "所有者空间", True),
                ("member@example.test", "验收成员", "成员空间", False),
                ("ui@example.test", "界面回归用户", "界面回归空间", False),
            ):
                args = [
                    sys.executable,
                    "-m",
                    "app.identity.bootstrap",
                    "--email",
                    email,
                    "--name",
                    name,
                    "--workspace",
                    space,
                    "--password-stdin",
                ]
                if platform:
                    args.append("--platform-admin")
                run(args, env, ROOT / "backend", password + "\n")
            run(
                [
                    sys.executable,
                    "-m",
                    "app.identity.bootstrap",
                    "--email",
                    "owner@example.test",
                    "--name",
                    "拒绝覆盖",
                    "--password-stdin",
                ],
                env,
                ROOT / "backend",
                password + "\n",
                expected=2,
            )
            report["checks"].append("独立初始化账号及重复初始化拒绝覆盖")
            # 限流测试使用独立随机密钥，仅删除由本次构造的两个键。
            limiter_settings = settings.model_copy(
                update={"session_secret": settings.session_secret.__class__(secret)}
            )
            limiter = RedisLoginLimiter(limiter_settings)
            keys = [
                "yugitalk:login:" + digest(secret, "rate-limit", value)
                for value in ("ip:stage2-test", "account:limit@example.test")
            ]
            redis = Redis.from_url(settings.redis_url.get_secret_value())
            try:
                for _ in range(10):
                    limiter("stage2-test", "limit@example.test")
                try:
                    limiter("stage2-test", "limit@example.test")
                except Exception as failure:
                    if getattr(failure, "status_code", None) != 429:
                        raise
                else:
                    raise RuntimeError("真实 Redis 限流未生效")
                report["checks"].append("真实 Redis：第 11 次同账号尝试返回 429")
            finally:
                redis.delete(*keys)
                redis.close()
            run([*compose, "up", "-d", "--wait", "--wait-timeout", "120"], env)
            if stage == 5:
                env["STAGE5_COMPOSE_PROJECT"] = f"yugitalk-stage{stage}-test"
                run([sys.executable, "scripts/check-stage5-recovery.py"], env)
                report["checks"].append(
                    "真实 PostgreSQL 与 SIGKILL：租约恢复、并发重复投递、只读工具账本复用、工单下线与预算核对"
                )
            run(
                ["npm.cmd" if os.name == "nt" else "npm", "run", f"test:stage{stage}"],
                env,
                ROOT / "frontend",
            )
            if stage >= 4:
                run([sys.executable, "scripts/check-stage4-rag.py"], env)
                report["checks"].append(
                    "真实 BGE-M3：固定中文检索评测、授权引用与删除传播"
                )
            with psycopg.connect(test_url) as db:
                hashes = db.execute("SELECT password_hash FROM users").fetchall()
                assert len(hashes) == 3 and all(
                    row[0].startswith("$argon2id$") for row in hashes
                )
                assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] > 0
                if stage >= 3:
                    assert (
                        db.execute(
                            "SELECT COUNT(*) FROM runs WHERE status IN ('queued', 'running')"
                        ).fetchone()[0]
                        == 0
                    )
                    assert (
                        db.execute(
                            "SELECT COUNT(*) FROM budgets WHERE reserved <> 0 OR spent < 0 OR spent > 100000"
                        ).fetchone()[0]
                        == 0
                    )
                    assert (
                        db.execute(
                            "SELECT COUNT(*) FROM runs WHERE status = 'succeeded'"
                        ).fetchone()[0]
                        >= 2
                    )
                    assert (
                        db.execute(
                            "SELECT COUNT(*) FROM runs WHERE status = 'cancelled'"
                        ).fetchone()[0]
                        >= 3
                    )
                    report["checks"].append(
                        "真实 Worker：多轮工具、并发上限、幂等、取消和预算释放"
                    )
            report["checks"].append("真实容器浏览器验证及 Argon2id、审计持久化")
        finally:
            try:
                run([*compose, "down", "--remove-orphans"], env)
            finally:
                if stage >= 4:
                    # 只清理脚本生成的随机桶和随机集合，绝不枚举或删除开发知识库。
                    from app.rag.embeddings import Embeddings, VectorIndex
                    from app.rag.storage import ObjectStorage

                    isolated = settings.model_copy(
                        update={
                            "storage_bucket": env["STAGE4_STORAGE_BUCKET"],
                            "vector_collection_prefix": env["STAGE4_COLLECTION_PREFIX"],
                        }
                    )
                    storage = ObjectStorage(isolated)
                    try:
                        pages = storage.client.get_paginator(
                            "list_objects_v2"
                        ).paginate(Bucket=storage.bucket)
                        for objects in pages:
                            for item in objects.get("Contents", []):
                                storage.remove(item["Key"])
                        storage.client.delete_bucket(Bucket=storage.bucket)
                    except Exception as failure:
                        from botocore.exceptions import ClientError

                        if (
                            not isinstance(failure, ClientError)
                            or failure.response["Error"]["Code"] != "NoSuchBucket"
                        ):
                            raise
                    vectors = VectorIndex(isolated)
                    collection = Embeddings(isolated).collection
                    if vectors.request(
                        "collections/has", {"collectionName": collection}
                    )["data"]["has"]:
                        vectors.request(
                            "collections/drop", {"collectionName": collection}
                        )
                if created:
                    admin.execute(
                        sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                            sql.Identifier(database)
                        )
                    )
                report["cleanup"] = True
                (
                    ROOT / f"docs/stages/artifacts/stage{stage}-integration.json"
                ).write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )
    print(f"阶段 {stage} 隔离集成验收完成，临时容器和数据库已清理。")


if __name__ == "__main__":
    # Windows 终端可能沿用非 UTF-8 编码；中文验收输出固定为 UTF-8。
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    main()

"""开发环境身份与个人工具迁移前备份关系库，迁移后核对原表行数，不执行恢复。"""

import argparse
import json
import re
import runpy
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """仅操作本项目开发关系库，完整保存迁移前备份与运行镜像标记。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    helper = runpy.run_path(str(ROOT / "scripts/prepare-tools-migration.py"))
    query, command = helper["query"], helper["command"]
    artifact = ROOT / "docs/stages/artifacts/registration-migration-validation.json"
    if args.verify:
        record = json.loads(artifact.read_text(encoding="utf-8"))
        revision = query("SELECT version_num FROM alembic_version")
        after = {
            name: int(query(f'SELECT count(*) FROM "{name}"')) for name in record["before_counts"]
        }
        if revision != "0013_private_tools" or after != record["before_counts"]:
            raise RuntimeError("迁移版本或原业务行数不一致，停止后续更新")
        record.update(after_schema=revision, original_rows_preserved=True, passed=True)
        artifact.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        print("迁移通过，全部原表行数一致；尚未初始化新管理员。")
        return
    if query("SELECT version_num FROM alembic_version") != "0010_tool_management":
        raise RuntimeError("当前关系库不是预期的迁移前版本")
    if int(query("SELECT count(*) FROM runs WHERE status IN ('queued','running')")):
        raise RuntimeError("当前有执行中的任务，请稍后进行迁移")
    if int(
        query(
            "SELECT count(*) FROM (SELECT lower(email) FROM users GROUP BY lower(email) "
            "HAVING count(*) > 1) AS duplicate_emails"
        )
    ):
        raise RuntimeError("既有邮箱存在大小写冲突，须人工处理，禁止自动合并身份")
    names = query(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
    ).splitlines()
    if any(not re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in names):
        raise RuntimeError("存在不受支持的表名")
    counts = {
        name: int(query(f'SELECT count(*) FROM "{name}"'))
        for name in names
        if name != "alembic_version"
    }
    filename = "auth-before-0013-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + ".dump"
    target = ROOT / "data/backups" / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    remote = "/tmp/" + filename
    command(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "pg_dump",
            "-U",
            "yugitalk",
            "-d",
            "yugitalk",
            "-Fc",
            "-f",
            remote,
        ]
    )
    command(["docker", "compose", "exec", "-T", "postgres", "pg_restore", "--list", remote])
    command(["docker", "compose", "cp", "postgres:" + remote, str(target)])
    command(["docker", "compose", "exec", "-T", "postgres", "rm", remote])
    images = {}
    for service, component in (("api", "backend"), ("frontend", "frontend")):
        container = command(["docker", "compose", "ps", "--all", "-q", service])
        if not container:
            # 用户已移除容器时无法观察此前运行ID，不能把新构建标签当作上一版本。
            continue
        identity = command(["docker", "inspect", container, "--format", "{{.Image}}"])
        command(["docker", "image", "tag", identity, f"yugitalk-{component}:auth-before-0013"])
        images[service] = identity
    record = {
        "before_schema": "0010_tool_management",
        "before_counts": counts,
        "backup": str(target.relative_to(ROOT)),
        "before_images": images,
        "backup_scope": "postgresql_only",
        "restore_performed": False,
        "passed": False,
    }
    artifact.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print("开发关系库备份及归档可读性核对完成，未执行恢复演练。")


if __name__ == "__main__":
    main()

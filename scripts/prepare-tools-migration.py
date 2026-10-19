"""工具迁移前保存关系库备份和表计数；只操作本项目开发 PostgreSQL。"""

import argparse
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def command(arguments: list[str]) -> str:
    """捕获容器操作输出，失败时只报告操作类型，不输出业务数据或环境配置。"""
    result = subprocess.run(
        arguments, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120
    )
    if result.returncode:
        raise RuntimeError("工具迁移准备失败：" + arguments[1])
    return result.stdout.strip()


def query(sql: str) -> str:
    """通过本项目容器中的本地数据库账户执行只读 SQL，不读取私有连接串。"""
    return command(
        [
            "docker",
            "compose",
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
            sql,
        ]
    )


def counts() -> dict[str, int]:
    """读取所有原有业务表的行数，供新增表迁移后的保留性核对。"""
    names = query(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
    ).splitlines()
    result = {}
    for name in names:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise RuntimeError("数据库表名超出受控范围")
        if name not in {"alembic_version", "managed_tools", "tool_versions"}:
            result[name] = int(query(f'SELECT count(*) FROM "{name}"'))
    return result


def main() -> None:
    """拒绝在任务执行中迁移，保存完整 PG dump 并校验归档目录可读取。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    artifact = ROOT / "docs/stages/artifacts/tools-migration-validation.json"
    if args.verify:
        record = json.loads(artifact.read_text(encoding="utf-8"))
        current = query("SELECT version_num FROM alembic_version")
        after = counts()
        if current != "0010_tool_management" or after != record["before_counts"]:
            raise RuntimeError("迁移版本或原业务表行数不一致，停止更新服务")
        assert query("SELECT count(*) FROM managed_tools") == "0"
        assert query("SELECT count(*) FROM tool_versions") == "0"
        record.update(
            {
                "after_schema": current,
                "after_counts": after,
                "original_rows_preserved": True,
                "passed": True,
            }
        )
        artifact.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print("工具迁移通过，原业务表行数一致，默认工具读取未新增业务记录。")
        return
    if int(query("SELECT count(*) FROM runs WHERE status IN ('queued','running')")):
        raise RuntimeError("有任务正在执行，请等待任务结束后再迁移")
    version = query("SELECT version_num FROM alembic_version")
    if version != "0009_custom_skills":
        raise RuntimeError("当前数据库不是预期的工具迁移前版本")
    before = counts()
    filename = "tools-before-0010-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + ".dump"
    local = ROOT / "data/backups" / filename
    local.parent.mkdir(parents=True, exist_ok=True)
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
    command(["docker", "compose", "cp", "postgres:" + remote, str(local)])
    if local.stat().st_size < 1000:
        raise RuntimeError("备份文件异常小，停止迁移")
    command(["docker", "compose", "exec", "-T", "postgres", "rm", remote])
    record = {
        "before_schema": version,
        "before_counts": before,
        "backup": str(local.relative_to(ROOT)),
        "backup_bytes": local.stat().st_size,
        "archive_list_verified": True,
        "full_restore_exercised": False,
    }
    (ROOT / "docs/stages/artifacts/tools-migration-validation.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("工具迁移前备份已保存，原有业务表计数已记录。")


if __name__ == "__main__":
    main()

"""维护命令默认只读盘点；只有显式参数才能清理已过期身份记录。"""

import argparse
import json
import sys
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import Engine, func, literal, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import load_settings
from app.core.database import create_database
from app.core.entities import Base, now
from app.identity.retention import (
    DEFAULT_BATCH_SIZE,
    MAX_BATCH_SIZE,
    cleanup_expired,
    inspect_expired,
    utc_cutoff,
)
from app.rag.models import Document


def inventory(
    engine: Engine, cutoff: datetime | None = None, batch_size: int = DEFAULT_BATCH_SIZE
) -> dict[str, Any]:
    """聚合各表记录、时间与物理空间；不加载正文，不扫描对象或向量存储。"""
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError("batch_size必须在1到1000之间")
    fixed = utc_cutoff(cutoff)
    tables = {}
    with engine.connect() as connection:
        postgres = connection.dialect.name == "postgresql"
        if postgres:
            connection = connection.execution_options(isolation_level="REPEATABLE READ")
        with connection.begin() as transaction:
            if postgres:
                connection.execute(text("SET TRANSACTION READ ONLY"))
                connection.execute(text("SET LOCAL statement_timeout = '5s'"))
            for table in sorted(Base.metadata.tables.values(), key=lambda table: table.name):
                stamp = next(
                    (
                        name
                        for name in ("created_at", "sent_at", "started_at", "expires_at")
                        if name in table.c
                    ),
                    None,
                )
                allocated = func.pg_total_relation_size(table.name) if postgres else literal(None)
                rows, oldest, size = cast(
                    tuple[int, datetime | None, int | None],
                    connection.execute(
                        select(
                            func.count(),
                            func.min(table.c[stamp]) if stamp else literal(None),
                            allocated,
                        ).select_from(table)
                    ).one(),
                )
                if oldest is not None:
                    oldest = (
                        oldest.replace(tzinfo=UTC)
                        if oldest.tzinfo is None
                        else oldest.astimezone(UTC)
                    )
                tables[table.name] = {
                    "rows": int(rows),
                    "timestamp_field": stamp,
                    "oldest_at": oldest.isoformat() if oldest is not None else None,
                    "allocated_bytes": int(size) if size is not None else None,
                }
            with Session(bind=connection) as db:
                preview = inspect_expired(db, fixed)
            declared_bytes = connection.scalar(
                select(func.coalesce(func.sum(Document.size), 0)).where(
                    Document.status != "deleted"
                )
            )
            transaction.rollback()
    for item in preview.values():
        item["next_batch"] = min(int(item["expired"] or 0), batch_size)
    return {
        "sampled_at": fixed.isoformat(),
        "batch_size": batch_size,
        "tables": tables,
        "auth_cleanup_preview": preview,
        "external_storage": {
            "measured": False,
            "declared_document_bytes_not_deleted": int(declared_bytes or 0),
        },
        "vector_store": {"measured": False},
    }


def main() -> int:
    """复用当前进程的数据库环境，输出聚合JSON并对异常消息脱敏。"""
    parser = argparse.ArgumentParser(description="默认只读盘点，不扫描外部存储")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只读盘点与清理预览，默认行为")
    mode.add_argument(
        "--apply-auth-cleanup", action="store_true", help="每表仅清理一批已过期身份记录"
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= MAX_BATCH_SIZE:
        parser.error("--batch-size必须在1到1000之间")
    engine = None
    try:
        engine = create_database(load_settings())
        fixed = now()
        report = inventory(engine, cutoff=fixed, batch_size=args.batch_size)
        report["mode"] = "apply_auth_cleanup" if args.apply_auth_cleanup else "dry_run"
        if args.apply_auth_cleanup:
            report["cleanup"] = cleanup_expired(engine, cutoff=fixed, batch_size=args.batch_size)
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except (ValueError, SQLAlchemyError) as error:
        print("维护执行失败：" + type(error).__name__, file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())

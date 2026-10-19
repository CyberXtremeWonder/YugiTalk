"""在独立临时 PostgreSQL 数据库验证所有者迁移，不修改业务库。"""

import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import create_engine, inspect, text
from app.core.config import load_settings


def migrate(url: str, revision: str) -> None:
    """以受控数据库配置运行迁移，失败时报告阶段而不回显凭证。"""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ROOT / "backend/alembic.ini"), "upgrade", revision],
        cwd=ROOT, env={**os.environ, "DATABASE_URL": url.replace("postgresql+psycopg://", "postgresql://", 1), "PYTHONUTF8": "1"},
        capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode:
        raise RuntimeError(f"隔离迁移 {revision} 失败，退出码 {result.returncode}；不回显可能包含配置值的诊断")


def main() -> None:
    """创建独立数据库，验证共享归所有者与个人归原用户，最后删除测试库。"""
    url = load_settings().database_url.get_secret_value().replace("postgresql://", "postgresql+psycopg://", 1)
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    name = "yugitalk_ownership_test_" + uuid4().hex[:12]
    test_url = admin.url.set(database=name).render_as_string(hide_password=False)
    test_engine = None
    try:
        with admin.connect() as db:
            db.execute(text(f'CREATE DATABASE "{name}"'))
        migrate(test_url, "0014_workspace_deletion")
        test_engine = create_engine(test_url)
        with test_engine.begin() as db:
            db.execute(text("INSERT INTO users(id,email,name,password_hash,active,platform_admin) VALUES ('u1','one@example.test','one','unused',true,false),('u2','two@example.test','two','unused',true,false)"))
            db.execute(text("INSERT INTO workspaces(id,name) VALUES ('s1','old one'),('s2','old two')"))
            db.execute(text("INSERT INTO memberships(workspace_id,user_id,role) VALUES ('s1','u1','owner'),('s2','u1','owner'),('s1','u2','member')"))
            db.execute(text("INSERT INTO knowledge_bases(id,workspace_id,name,created_at) VALUES ('b1','s1','first',now()),('b2','s2','second',now())"))
            db.execute(text("INSERT INTO memory_preferences(workspace_id,user_id,enabled) VALUES ('s1','u2',true),('s2','u2',false)"))
        migrate(test_url, "head")
        with test_engine.connect() as db:
            assert db.execute(text("SELECT DISTINCT account_id FROM knowledge_bases")).scalars().all() == ["u1"]
            assert db.execute(text("SELECT count(*) FROM knowledge_bases")).scalar() == 2
            assert db.execute(text("SELECT account_id,enabled FROM memory_preferences")).all() == [("u2", False)]
            assert not {"workspaces", "memberships"}.intersection(inspect(db).get_table_names())
        print("用户归属 PostgreSQL 隔离迁移验证通过")
    finally:
        if test_engine is not None:
            test_engine.dispose()
        with admin.connect() as db:
            db.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


if __name__ == "__main__":
    main()

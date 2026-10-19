"""比较用户归属迁移前后的数量、归属与文件索引元数据。"""

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import load_settings
from sqlalchemy import create_engine, inspect, text


def main() -> None:
    """迁移前保存比对数据，迁移后逐条校验，不记录凭证和资料正文。"""
    engine = create_engine(load_settings().database_url.get_secret_value().replace("postgresql://", "postgresql+psycopg://", 1))
    path = ROOT / ".cache/user-ownership-before.json"
    with engine.connect() as db:
        schema = inspect(db)
        if sys.argv[-1] == "before":
            owners = dict(db.execute(text("SELECT workspace_id,user_id FROM memberships WHERE role='owner'")).all())
            personal = {"conversations":"owner_id", "runs":"owner_id", "tickets":"owner_id", "memories":"user_id", "tool_submissions":"submitter_id", "audit_events":"actor_id"}
            result = {"counts":{}, "ownership":{}, "documents":{}}
            for table in schema.get_table_names():
                result["counts"][table] = db.execute(text(f'SELECT count(*) FROM "{table}"')).scalar()
                columns = [column["name"] for column in schema.get_columns(table)]
                if "workspace_id" not in columns or "id" not in columns or table == "workspaces":
                    continue
                expected = {}
                for row in db.execute(text(f'SELECT * FROM "{table}"')).mappings():
                    target = owners.get(row["workspace_id"])
                    if table in personal and row["workspace_id"]:
                        target = row[personal[table]]
                    if table == "managed_tools" and row.get("owner_id"):
                        target = row["owner_id"]
                    expected[row["id"]] = target
                result["ownership"][table] = expected
            result["documents"] = {row["id"]:{key:row[key] for key in ("object_key","content_hash","collection_name","embedding_version","index_version")} for row in db.execute(text("SELECT * FROM documents")).mappings()}
            path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
            print("迁移前比对数据已保存")
        else:
            original = json.loads(path.read_text(encoding="utf-8"))
            assert not {"workspaces","memberships"}.intersection(schema.get_table_names())
            for table,count in original["counts"].items():
                if table in {"workspaces","memberships","memory_preferences","budgets","alembic_version"}:
                    continue
                assert db.execute(text(f'SELECT count(*) FROM "{table}"')).scalar() == count, table
            for table,expected in original["ownership"].items():
                actual = dict(db.execute(text(f'SELECT id,account_id FROM "{table}"')).all())
                assert actual == expected, table
            actual = {row["id"]:{key:row[key] for key in ("object_key","content_hash","collection_name","embedding_version","index_version")} for row in db.execute(text("SELECT * FROM documents")).mappings()}
            assert actual == original["documents"]
            report = {"passed":True,"schema":"0015_user_ownership","counts_before":original["counts"],"ownership_verified":True,"document_metadata_unchanged":True,"remote_restore_drill":"not_performed"}
            (ROOT / "docs/stages/artifacts/user-ownership-validation.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
            print("数量、用户归属和文件索引元数据校验通过")
    engine.dispose()


if __name__ == "__main__":
    main()

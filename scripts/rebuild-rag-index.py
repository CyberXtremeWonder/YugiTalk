"""显式选择一个资料升级混合索引，默认仅查看，不自动迁移所有业务资料。"""

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings  # noqa: E402
from app.core.database import create_database  # noqa: E402
from app.rag.embeddings import Embeddings  # noqa: E402
from app.rag.models import Document  # noqa: E402
from app.rag.rebuild import rebuild_document  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402


def main() -> None:
    """验证明确资料标识后查看计划；带 apply 才写入新集合并切换资料指针。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--document", required=True, type=UUID)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    engine = create_database(settings)
    try:
        # 导入元数据依赖，保证单独启动 CLI 时关系映射完整。
        import app.main  # noqa: F401

        if args.apply:
            result = rebuild_document(engine, settings, str(args.document))
        else:
            with Session(engine) as db:
                doc = db.get(Document, str(args.document))
                if not doc:
                    raise ValueError("document_rebuild_unavailable")
                result = {
                    "document_id": doc.id,
                    "status": doc.status,
                    "already_current": doc.collection_name == Embeddings(settings).collection,
                    "apply_required": True,
                }
        print(json.dumps(result, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

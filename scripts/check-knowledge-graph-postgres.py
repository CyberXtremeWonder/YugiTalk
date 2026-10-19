"""仅连接固定本机临时库，验证真实迁移、图谱发布、召回与外键清理。"""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core import model_registry  # noqa: F401
from app.core.config import Settings
from app.identity.models import User
from app.rag.embeddings import Embeddings
from app.rag.graph import (
    Extraction,
    build_graph,
    clear_graph,
    graph_recall,
    queue_graph,
)
from app.rag.graph_models import GraphBuild, GraphFact
from app.rag.model import StructuredModel
from app.rag.models import Chunk, Document, KnowledgeBase
from app.rag.retrieval import document_scope
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.orm import Session

URL = "postgresql+psycopg://graph_verify:graph-verify-local-only@127.0.0.1:55437/graph_verify"


def migrate(revision: str, downgrade: bool = False) -> None:
    """迁移固定测试库，输出不包含真实业务配置或凭证。"""
    environment = {
        **os.environ,
        "DATABASE_URL": URL.replace("postgresql+psycopg://", "postgresql://"),
        "APP_ENV": "test",
        "MODEL_PROVIDER": "mock",
        "PYTHONUTF8": "1",
    }
    subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "downgrade" if downgrade else "upgrade",
            revision,
        ],
        cwd=ROOT / "backend",
        env=environment,
        check=True,
        capture_output=True,
    )


def main() -> None:
    """临时库须为空；执行全迁移和来源闭环后保存可复现报告。"""
    engine = create_engine(URL, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT current_database()")).scalar()
                == "graph_verify"
            )
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
                    )
                ).scalar()
                == 0
            ), "仅允许全新临时库，拒绝覆盖已有数据"
        migrate("head")
        settings = Settings(_env_file=None, app_env="test", embedding_provider="mock")
        embedding = Embeddings(settings)
        with Session(engine) as db:
            owner = User(
                email="graph-verify@example.test",
                name="隔离验证",
                password_hash="unused",
            )
            db.add(owner)
            db.flush()
            base = KnowledgeBase(account_id=owner.id, name="隔离图谱")
            db.add(base)
            db.flush()
            documents = []
            for number, content in enumerate(
                ("支付服务使用DB1。", "订单服务依赖支付服务。")
            ):
                doc = Document(
                    account_id=owner.id,
                    knowledge_base_id=base.id,
                    filename=f"test{number}.txt",
                    content_hash=str(number),
                    object_key="unused",
                    size=len(content),
                    status="ready",
                    index_version=1,
                    embedding_version=embedding.version,
                    collection_name=embedding.collection,
                )
                db.add(doc)
                db.flush()
                db.add(
                    Chunk(
                        document_id=doc.id, index_version=1, ordinal=1, content=content
                    )
                )
                queue_graph(db, doc)
                documents.append(doc.id)
            owner_id, base_id = owner.id, base.id
            db.commit()

        calls = []

        async def extract(
            self: StructuredModel,
            schema: type[Extraction],
            purpose: str,
            payload: dict[str, str],
            deadline: float,
        ) -> Extraction:
            """替换模型响应，保留真实数据库锁、发布和来源校验。"""
            calls.append(payload["text"])
            source, target, relation = (
                ("支付服务", "DB1", "使用")
                if "DB1" in payload["text"]
                else ("订单服务", "支付服务", "依赖")
            )
            return Extraction.model_validate(
                {
                    "entities": [
                        {"name": source, "kind": "system"},
                        {"name": target, "kind": "system"},
                    ],
                    "facts": [
                        {
                            "source": 0,
                            "target": 1,
                            "relation": relation,
                            "quote": payload["text"],
                        }
                    ],
                }
            )

        real_mode = settings.model_copy(update={"model_provider": "openai_compatible"})
        with patch.object(StructuredModel, "call", extract):
            for key in documents:
                build_graph(engine, real_mode, key, 1)
                build_graph(engine, real_mode, key, 1)
        assert len(calls) == 2
        with Session(engine) as db:
            assert all(db.get(GraphBuild, key).status == "ready" for key in documents)
            scope = document_scope(db, settings, owner_id, owner_id, base_id)
            hits = graph_recall(db, scope, "DB1故障", 24)
            assert len(hits) == 2
            assert (
                graph_recall(
                    db, {key: (2, embedding.collection) for key in documents}, "DB1", 24
                )
                == []
            )
            doc = db.get(Document, documents[1])
            doc.status = "deleting"
            db.commit()
            scope = document_scope(db, settings, owner_id, owner_id, base_id)
            assert len(graph_recall(db, scope, "DB1", 24)) == 1
            clear_graph(db, documents[1])
            db.execute(delete(GraphBuild).where(GraphBuild.document_id == documents[1]))
            db.execute(delete(Chunk).where(Chunk.document_id == documents[1]))
            db.commit()
            assert len(list(db.scalars(select(GraphFact)))) == 1
        migrate("0016_task_traces", downgrade=True)
        migrate("head")
        with Session(engine) as db:
            assert len(list(db.scalars(select(Document)))) == 2
        report = {
            "passed": True,
            "database": "临时 PostgreSQL 16，内存存储",
            "checks": [
                "全链路迁移至0017",
                "真实外键关系发布",
                "重复任务幂等",
                "跨文档两跳召回",
                "过期代次隔离",
                "删除后立即隔离",
                "外键清理保留其他来源",
                "图谱迁移降级与升级保留文档",
            ],
            "real_model_calls": 0,
            "business_data_changed": False,
        }
        target = ROOT / "docs/stages/artifacts/knowledge-graph-postgres.json"
        target.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("PostgreSQL 图谱验证通过")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

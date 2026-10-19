"""验证图谱原文闭环、两跳召回、租约、版本和权限隔离。"""

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_identity import login
from test_knowledge import base

from app.core.entities import now
from app.providers.client import ProviderFailure
from app.rag.graph import (
    Extraction,
    build_graph,
    fuse_graph,
    graph_recall,
    matches,
    queue_graph,
    sweep_graph,
    validate_extraction,
)
from app.rag.graph_models import GraphBuild, GraphEntity, GraphFact
from app.rag.indexing import cleanup_document, index_document, retrieve
from app.rag.models import Chunk, Document
from app.rag.pipeline import RagPipeline
from app.rag.retrieval import document_scope

pytest_plugins = ["test_identity", "test_knowledge"]


def extraction(source="支付服务", target="DB1", relation="使用", quote="支付服务使用DB1。"):
    """构造可核验的结构化输出，不调用付费模型。"""
    return Extraction.model_validate(
        {
            "entities": [{"name": source, "kind": "system"}, {"name": target, "kind": "system"}],
            "facts": [{"source": 0, "target": 1, "relation": relation, "quote": quote}],
        }
    )


def upload(client, headers, base_id, text):
    """通过真实接口导入，外部向量与对象存储由测试替身提供。"""
    response = client.post(
        f"/api/v1/knowledge-bases/{base_id}/documents?filename=graph.txt",
        content=text.encode(),
        headers=headers,
    )
    assert response.status_code == 202
    key = response.json()["id"]
    index_document(client.app.state.database, client.app.state.settings, key, 1)
    return key


def build(client, monkeypatch, key, data):
    """替换结构化模型但执行真实图谱发布流程。"""

    async def call(self, schema, purpose, payload, deadline):
        """返回确定性关系，用于检查抽取后约束。"""
        assert purpose == "graph"
        return data

    monkeypatch.setattr("app.rag.graph.StructuredModel.call", call)
    settings = client.app.state.settings.model_copy(update={"model_provider": "openai_compatible"})
    build_graph(client.app.state.database, settings, key, 1)


@pytest.mark.parametrize("mutation", ["quote", "endpoint", "name", "alias", "missing_name"])
def test_reject_unverifiable_facts(mutation):
    """伪造引文、无效端点和未出现在证据中的实体均禁止发布。"""
    data = extraction()
    if mutation == "quote":
        data.facts[0].quote = "支付服务依赖DB1。"
    elif mutation == "endpoint":
        data.facts[0].target = 9
    elif mutation == "name":
        data.entities[0].name = "订单服务"
    elif mutation == "alias":
        data.entities[0].aliases = ["虚构别名"]
    else:
        data.facts[0].quote = "使用DB1"
    with pytest.raises(ProviderFailure, match="graph_evidence_invalid"):
        validate_extraction("支付服务使用DB1。", data)


def test_two_hop_sources_and_deletion(client, storage, monkeypatch):
    """DB1 经支付服务找到另一文档订单服务，删除后路径立即失效。"""
    identity, headers, base_id = base(client)
    first = upload(client, headers, base_id, "支付服务使用DB1。")
    second = upload(client, headers, base_id, "订单服务依赖支付服务。")
    build(client, monkeypatch, first, extraction())
    build(
        client,
        monkeypatch,
        second,
        extraction("订单服务", "支付服务", "依赖", "订单服务依赖支付服务。"),
    )
    engine, settings = client.app.state.database, client.app.state.settings
    owner = identity["user"]["id"]
    with Session(engine) as db:
        scope = document_scope(db, settings, owner, owner, base_id)
        hits = graph_recall(db, scope, "DB1故障影响哪些服务？", 24)
        assert {db.get(Chunk, hit["id"]).document_id for hit in hits} == {first, second}
        # 空普通检索也能通过图谱找到真实 SQL 原文。
        fused, count = fuse_graph(db, settings, scope, "DB1", [])
        assert count == 2 and all(hit["rrf_score"] > 0 for hit in fused)
    assert client.delete(f"/api/v1/documents/{second}", headers=headers).status_code == 202
    with Session(engine) as db:
        scope = document_scope(db, settings, owner, owner, base_id)
        assert len(graph_recall(db, scope, "DB1", 24)) == 1
    cleanup_document(engine, settings, second)
    with Session(engine) as db:
        assert not list(db.scalars(select(GraphFact).where(GraphFact.document_id == second)))
        assert list(db.scalars(select(GraphFact).where(GraphFact.document_id == first)))


def test_failed_graph_keeps_rag_and_can_retry(client, storage, monkeypatch):
    """无效抽取不发布半成品，普通索引可用且重试不增加文档版本。"""
    identity, headers, base_id = base(client)
    key = upload(client, headers, base_id, "支付服务使用DB1。")
    invalid = extraction()
    invalid.facts[0].quote = "编造原文"
    build(client, monkeypatch, key, invalid)
    engine, settings = client.app.state.database, client.app.state.settings
    with Session(engine) as db:
        assert db.get(Document, key).status == "ready"
        assert db.get(GraphBuild, key).status == "failed"
        assert not list(db.scalars(select(GraphFact)))
        owner = identity["user"]["id"]
        assert retrieve(db, settings, owner, owner, base_id, "支付服务")
    assert client.post(f"/api/v1/documents/{key}/graph", headers=headers).status_code == 202
    build(client, monkeypatch, key, extraction())
    result = client.get(f"/api/v1/documents/{key}/graph").json()
    assert result["status"] == "ready" and result["index_version"] == 1
    assert result["facts"][0]["quote"] == "支付服务使用DB1。"
    build(client, monkeypatch, key, extraction())
    with Session(engine) as db:
        assert len(list(db.scalars(select(GraphFact)))) == 1


def test_cross_owner_base_version_and_csrf(client, storage, monkeypatch):
    """图谱预览和构建遵守权限；其他知识库及过期代次不可召回。"""
    identity, headers, base_id = base(client)
    key = upload(client, headers, base_id, "支付服务使用DB1。")
    build(client, monkeypatch, key, extraction())
    assert client.post(f"/api/v1/documents/{key}/graph").status_code == 403
    engine, settings = client.app.state.database, client.app.state.settings
    with Session(engine) as db:
        assert graph_recall(db, {}, "DB1", 24) == []
        assert graph_recall(db, {key: (2, "unused")}, "DB1", 24) == []
    other = client.post(
        "/api/v1/me/knowledge-bases", json={"name": "另一个库"}, headers=headers
    ).json()
    with Session(engine) as db:
        owner = identity["user"]["id"]
        scope = document_scope(db, settings, owner, owner, other["id"])
        assert graph_recall(db, scope, "DB1", 24) == []
    login(client, "other@example.test")
    assert client.get(f"/api/v1/documents/{key}/graph").status_code in {403, 404}
    assert client.post(f"/api/v1/documents/{key}/graph", headers=headers).status_code == 403


def test_delete_during_extraction_cannot_publish(client, storage, monkeypatch):
    """模型调用期间删除文档，旧构建不能发布图谱。"""
    _, headers, base_id = base(client)
    key = upload(client, headers, base_id, "支付服务使用DB1。")
    engine = client.app.state.database

    async def call(self, *args):
        """在模型返回前模拟删除提交。"""
        with Session(engine) as db:
            db.get(Document, key).status = "deleting"
            db.commit()
        return extraction()

    monkeypatch.setattr("app.rag.graph.StructuredModel.call", call)
    settings = client.app.state.settings.model_copy(update={"model_provider": "openai_compatible"})
    build_graph(engine, settings, key, 1)
    with Session(engine) as db:
        assert not list(db.scalars(select(GraphFact)))
        assert db.get(Document, key).status == "deleting"


def test_scheduler_recovers_and_stops_after_three_attempts(client, storage, monkeypatch):
    """队列投递可重复；租约过期有界恢复，达到次数上限后停止。"""
    _, headers, base_id = base(client)
    key = upload(client, headers, base_id, "支付服务使用DB1。")
    engine, settings = client.app.state.database, client.app.state.settings
    sent = []
    monkeypatch.setattr("app.rag.graph.dispatch_graph", lambda *args: sent.append(args))
    sweep_graph(engine, settings)
    sweep_graph(engine, settings)
    assert sent == [(key, 1)]
    for attempts, expected in [(1, "queued"), (3, "failed")]:
        with Session(engine) as db:
            row = db.get(GraphBuild, key)
            row.status, row.attempts = "building", attempts
            row.lease_until = now() - timedelta(seconds=1)
            row.dispatched_at = None
            db.commit()
        sweep_graph(engine, settings)
        with Session(engine) as db:
            assert db.get(GraphBuild, key).status == expected


def test_pipeline_graph_only_returns_original_sources(client, storage, monkeypatch):
    """普通召回为空时图谱候选仍进入评分与可核验引用流程。"""
    identity, headers, base_id = base(client)
    key = upload(client, headers, base_id, "支付服务使用DB1。")
    build(client, monkeypatch, key, extraction())

    async def empty(*args):
        """模拟向量与关键词检索遗漏。"""
        return [], {}

    monkeypatch.setattr("app.rag.pipeline.recall_queries", empty)
    owner = identity["user"]["id"]
    state = RagPipeline(
        client.app.state.database,
        client.app.state.settings,
        owner,
        owner,
        base_id,
        lambda *args: None,
        lambda state: None,
    ).run("DB1")
    assert state["route"] == "answer"
    assert state["sources"][0]["content"] == "支付服务使用DB1。"
    assert state["sources"][0]["evidence_id"].startswith("E-")
    assert state["trace"][1]["graph_candidates"] == 1


def test_mock_disabled_and_name_boundaries(client, storage):
    """演示模式不伪造关系，关闭后不领取任务，英文名称不命中其他词。"""
    _, headers, base_id = base(client)
    key = upload(client, headers, base_id, "支付服务使用DB1。")
    settings, engine = client.app.state.settings, client.app.state.database
    build_graph(engine, settings.model_copy(update={"rag_graph_enabled": False}), key, 1)
    with Session(engine) as db:
        assert db.get(GraphBuild, key).status == "queued"
    build_graph(engine, settings, key, 1)
    with Session(engine) as db:
        assert db.get(GraphBuild, key).mode == "mock"
        assert not list(db.scalars(select(GraphEntity)))
    assert matches("DB1", "DB1故障")
    assert not matches("DB1", "DB123")


def test_graph_query_failure_keeps_dense_hits(client, storage, monkeypatch):
    """图谱存储查询故障回退原排名，不吞掉业务权限异常。"""
    from sqlalchemy.exc import OperationalError

    def fail(*args):
        """模拟图谱查询服务异常。"""
        raise OperationalError("test", {}, Exception())

    monkeypatch.setattr("app.rag.graph.graph_recall", fail)
    with Session(client.app.state.database) as db:
        hits = [{"id": "original", "rrf_score": 0.5}]
        assert fuse_graph(db, client.app.state.settings, {}, "DB1", hits) == (hits, -1)


def test_oversized_graph_does_not_publish_partial_data(client, storage):
    """超出首版抽取上限时明确失败，普通文档状态保持可用。"""
    _, headers, base_id = base(client)
    key = upload(client, headers, base_id, "支付服务使用DB1。")
    engine = client.app.state.database
    with Session(engine) as db:
        for number in range(64):
            db.add(Chunk(document_id=key, index_version=1, ordinal=number + 2, content="额外文本"))
        queue_graph(db, db.get(Document, key))
        db.commit()
    build_graph(engine, client.app.state.settings, key, 1)
    with Session(engine) as db:
        assert db.get(GraphBuild, key).error_code == "graph_document_limit"
        assert db.get(Document, key).status == "ready"


def test_migration_round_trip_preserves_documents(client, storage):
    """在隔离数据库实际执行升降级，验证外键和旧文档保留。"""
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    _, headers, base_id = base(client)
    key = upload(client, headers, base_id, "支付服务使用DB1。")
    path = Path(__file__).parents[1] / "migrations/versions/0017_knowledge_graph.py"
    spec = importlib.util.spec_from_file_location("graph_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = client.app.state.database
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module.downgrade()
            assert "graph_facts" not in inspect(connection).get_table_names()
            module.upgrade()
            assert len(inspect(connection).get_foreign_keys("graph_facts")) == 4
    with Session(engine) as db:
        assert db.get(Document, key).status == "ready"
        queue_graph(db, db.get(Document, key))
        db.commit()
    build_graph(engine, client.app.state.settings, key, 1)
    with Session(engine) as db:
        assert db.get(GraphBuild, key).status == "ready"


def test_reindex_invalidates_and_cleans_graph(client, storage, monkeypatch):
    """重新索引立即隔离旧图谱并清除旧外键，发布新代次排队任务。"""
    identity, headers, base_id = base(client)
    key = upload(client, headers, base_id, "支付服务使用DB1。")
    build(client, monkeypatch, key, extraction())
    assert client.post(f"/api/v1/documents/{key}/retry", headers=headers).status_code == 200
    assert client.get(f"/api/v1/documents/{key}/graph").json()["status"] == "unavailable"
    engine, settings = client.app.state.database, client.app.state.settings
    index_document(engine, settings, key, 2)
    with Session(engine) as db:
        assert db.get(Document, key).status == "ready"
        assert db.get(GraphBuild, key).index_version == 2
        assert db.get(GraphBuild, key).status == "queued"
        assert not list(db.scalars(select(GraphFact)))

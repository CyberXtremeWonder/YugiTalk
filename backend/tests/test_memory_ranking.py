"""候选评分、完整条目预算、记忆接口与迁移的独立回归。"""

import json
from datetime import UTC, datetime, timedelta
from importlib import import_module

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_agents import setup_task
from test_identity import ORIGIN, login

from app.core.config import Settings
from app.memory.context import build_messages
from app.memory.models import Memory, MemoryPreference
from app.memory.ranking import rank_memories, select_memories
from app.tasks.execution import Execution
from app.tasks.models import Run

pytest_plugins = ["test_identity"]
REFERENCE = datetime(2026, 11, 13, tzinfo=UTC)


def memory(key, *, content="记忆正文", importance=3, age=0):
    return Memory(id=key, content=content, importance=importance,
                  created_at=REFERENCE - timedelta(days=age))


def test_components_weights_and_stable_ties():
    candidates = [memory("b", importance=5, age=90), memory("a", importance=1)]
    ranked = rank_memories(candidates, [0.2, 0.9], REFERENCE, (0.5, 0.4, 0.1), 90)
    assert [r.memory.id for r in ranked] == ["a", "b"]
    assert ranked[1].recency == pytest.approx(0.5)
    assert ranked[1].importance == 1
    assert ranked[1].score == pytest.approx(0.55)
    # 单独调整重要性权重应改变选择，而非仅增加一个无效字段。
    assert rank_memories(candidates, [0.2, 0.9], REFERENCE, (0, 1, 0), 90)[0].memory.id == "b"
    tied = rank_memories(candidates[::-1], [0.4, 0.4], REFERENCE, (1, 0, 0), 90)
    assert [r.memory.id for r in tied] == ["a", "b"]


def test_utc_naive_future_timestamp_and_similarity_bounds():
    rows = [memory("past", age=90), memory("future", age=-2)]
    rows[0].created_at = rows[0].created_at.replace(tzinfo=None)
    ranked = rank_memories(rows, [-0.5, 1.01], REFERENCE.replace(tzinfo=None), (1, 0, 0), 90)
    assert ranked[0].relevance == ranked[0].recency == 1
    assert ranked[1].relevance == 0 and ranked[1].recency == 0.5


def test_budget_never_splits_conditions_and_counts_newlines():
    rows = [memory("a", content="中" * 700), memory("b", content="不要使用旧库"),
            memory("c", content="使用新库"), memory("d", content="尾")]
    ranked = rank_memories(rows, [1, 0.9, 0.8, 0.7], REFERENCE, (1, 0, 0), 90)
    budget = len("不要使用旧库\n使用新库".encode())
    selected = select_memories(ranked, budget)
    assert [r.memory.id for r in selected] == ["b", "c"]
    assert select_memories(ranked, 0) == []


@pytest.mark.parametrize("weights", [(0, 0, 0), (0.5, 0.5, 0.5), (-1, 1, 1),
                                     (float("nan"), 0, 1), (float("inf"), 0, 0)])
def test_invalid_weights_rejected(weights):
    with pytest.raises(ValueError, match="MEMORY_RANKING_WEIGHTS"):
        Settings(app_env="test", memory_ranking_weights=weights)


def test_importance_api_default_preservation_validation_and_isolation(client):
    identity = login(client)
    headers = {**ORIGIN, "X-CSRF-Token": identity["csrf_token"]}
    path = "/api/v1/me/memories"
    payload = {"content": "默认中文", "confirmed": True}
    assert client.post(path, json=payload, headers=headers).json()["importance"] == 3
    row = client.post(path, json={**payload, "importance": 5}, headers=headers).json()
    target = f"/api/v1/memories/{row['id']}"
    assert client.patch(target, json=payload, headers=headers).json()["importance"] == 5
    assert client.patch(target, json={**payload, "importance": 1},
                        headers=headers).json()["importance"] == 1
    for value in (0, 6, 1.5, True, None):
        assert client.post(path, json={**payload, "importance": value},
                           headers=headers).status_code == 422
    other = login(client, "other@example.test")
    assert client.get(path).json()["items"] == []
    assert client.patch(target, json={**payload, "importance": 5}, headers={
        **ORIGIN, "X-CSRF-Token": other["csrf_token"],
    }).status_code == 404


def test_importance_migration_preserves_rows_and_checks_range():
    migration = import_module("migrations.versions.0027_memory_importance")
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT)")
        connection.exec_driver_sql("INSERT INTO memories VALUES ('old', '保留正文')")
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert connection.exec_driver_sql("SELECT content, importance FROM memories").one() == (
            "保留正文", 3,
        )
        connection.exec_driver_sql("UPDATE memories SET importance = 5")
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.exec_driver_sql("UPDATE memories SET importance = 6")
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert connection.exec_driver_sql("SELECT * FROM memories").one() == ("old", "保留正文")
    engine.dispose()


def test_execution_passes_ranking_settings_without_embedding_call(client, monkeypatch):
    _, _, task, _ = setup_task(client, "比较数据库配置", token_budget=100000)
    settings = client.app.state.settings
    settings.memory_ranking_weights = (0, 1, 0)
    monkeypatch.setattr("app.memory.context.Embeddings.encode",
                        lambda *args: pytest.fail("相关性权重为零时不应请求嵌入"))
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        db.add_all([
            Memory(account_id=run.account_id, user_id=run.owner_id,
                   content="次要偏好", importance=1),
            Memory(account_id=run.account_id, user_id=run.owner_id,
                   content="重要偏好", importance=5),
        ])
        db.commit()
    Execution(client.app.state.database, settings, task["id"]).execute()
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        assert run.status == "succeeded", run.error_code
        messages = run.checkpoint["state"]["messages"]
        data = next(json.loads(m["content"])["data"] for m in messages
                    if '"context_kind": "user_preferences"' in str(m.get("content")))
        assert data == "重要偏好\n次要偏好"


@pytest.mark.parametrize("embedding_failure", [False, True])
def test_context_ranks_authorized_candidates_and_traces_degradation(client, monkeypatch,
                                                                  embedding_failure):
    _, _, task, _ = setup_task(client, "数据库选择")
    settings = client.app.state.settings.model_copy(update={
        "memory_ranking_weights": (0.8, 0.2, 0.0),
    })
    details = []
    requests = []
    monkeypatch.setattr("app.observability.traces.trace_details", lambda **kw: details.append(kw))

    def encode(self, texts):
        requests.append(texts)
        if embedding_failure:
            raise httpx.ConnectError("private endpoint must not enter trace")
        return [[1, 0], [0, 1], [1, 0]]

    monkeypatch.setattr("app.memory.context.Embeddings.encode", encode)
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        db.add_all([
            Memory(id="old", account_id=run.account_id, user_id=run.owner_id,
                   content="低相关的重要偏好", importance=5,
                   created_at=run.created_at - timedelta(days=1)),
            Memory(id="new", account_id=run.account_id, user_id=run.owner_id,
                   content="数据库使用 PostgreSQL", importance=1, created_at=run.created_at),
            Memory(id="foreign", account_id=run.account_id, user_id="foreign-user",
                   content="FOREIGN_PRIVATE", importance=5),
        ])
        db.flush()
        messages = build_messages(db, run, settings=settings)
        content = next(json.loads(m["content"])["data"] for m in messages
                       if '"context_kind": "user_preferences"' in str(m["content"]))
        assert content.startswith("低相关" if embedding_failure else "数据库")
        assert "FOREIGN_PRIVATE" not in json.dumps(requests + messages)
        assert details[-1]["memory_relevance_status"] == (
            "unavailable" if embedding_failure else "ok"
        )
        assert "private endpoint" not in json.dumps(details)
        db.add(MemoryPreference(account_id=run.account_id, user_id=run.owner_id, enabled=False))
        db.flush()
        requests.clear()
        disabled = build_messages(db, run, settings=settings)
        assert not requests and "user_preferences" not in json.dumps(disabled)

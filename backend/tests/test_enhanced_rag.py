"""七步 RAG 的次数、并发、截止时间、证据身份及人工恢复边界回归。"""

import asyncio
import copy
import json
import time
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from app.chat.models import Message
from app.core.config import Settings
from app.core.entities import now
from app.rag.contracts import EvidenceGrade, QueryPlan, Rewrite
from app.rag.embeddings import Embeddings, bounded_post
from app.rag.indexing import index_document, retrieve
from app.rag.model import StructuredModel
from app.rag.models import Chunk, Document, ParentChunk
from app.rag.pipeline import RagPipeline, local_plan
from app.rag.rerank import Reranker, _breakers
from app.rag.retrieval import document_scope, materialize, pack_evidence, recall_queries, rrf
from app.tasks.execution import Execution
from app.tasks.models import Budget, Run
from app.tasks.reliability import sweep
from app.identity.models import User
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from test_agents import setup_task
from test_identity import ORIGIN, login
from test_knowledge import base

pytest_plugins = ["test_identity", "test_knowledge"]


def uploaded(client, text="Milvus 保存向量，PostgreSQL 保存元数据。"):
    """建立已完成索引的隔离知识库，返回可授权的测试范围。"""
    identity, headers, key = base(client)
    doc = client.post(
        f"/api/v1/knowledge-bases/{key}/documents?filename=层级.md",
        content=text.encode(),
        headers=headers,
    ).json()
    index_document(client.app.state.database, client.app.state.settings, doc["id"], 1)
    return identity, headers, key, doc


def rag_task(client, headers, base_id, question):
    """建立知识库任务，停掉准备阶段的普通任务以保持并发限制。"""
    task_headers, body, original, _ = setup_task(client, token_budget=100000)
    client.post(f"/api/v1/runs/{original['id']}/cancel", headers=task_headers)
    task_headers["Idempotency-Key"] = str(uuid4())
    task = client.post(
        "/api/v1/runs",
        json={**body, "knowledge_base_id": base_id, "message": question},
        headers=task_headers,
    )
    assert task.status_code == 202
    return task.json(), task_headers


@pytest.mark.parametrize(
    "payload",
    [
        {"complexity": "complex", "sub_questions": ["只有一个"]},
        {"complexity": "complex", "sub_questions": ["重复", "重复"]},
        {"complexity": "simple", "sub_questions": ["多余"]},
        {"complexity": "complex", "sub_questions": [str(i) for i in range(5)]},
    ],
)
def test_plan_rejects_unbounded_output(payload):
    """拒绝一支、五支、重复问题及简单问题额外并行。"""
    with pytest.raises(ValidationError):
        QueryPlan.model_validate(payload)


def test_local_complexity_and_rrf_score_units():
    """短事实绕过规划模型，RRF 根据名次而非跨度量原分数合成。"""
    assert local_plan("向量在哪里？").complexity == "simple"
    assert len(local_plan("比较 Milvus 和 PostgreSQL 的区别").sub_questions) == 2
    result = rrf(
        [
            [{"id": "a", "distance": 0.9}, {"id": "b", "distance": 0.8}],
            [{"id": "b", "distance": 5000}, {"id": "a", "distance": 1}],
        ],
        5,
    )
    assert result[0]["rrf_score"] == result[1]["rrf_score"]


def test_parallel_recall_limit_and_failure_cancellation():
    """四个子问题真实并发，任一存储故障取消兄弟分支且不伪装成功。"""
    settings = Settings(
        _env_file=None, app_env="test", embedding_provider="mock", rag_parallel_queries=2
    )
    active, peak, completed = 0, 0, []

    class Index:
        """提供可控延迟与故障，观察异步召回是否重叠执行。"""

        async def recall(self, *args):
            """同时模拟密集与稀疏网络调用，保持统计对称。"""
            nonlocal active, peak
            active += 1
            peak = max(active, peak)
            try:
                await asyncio.sleep(0.025)
                completed.append(args[5])
                return [{"id": "doc", "distance": 1.0}]
            finally:
                active -= 1

    collection = Embeddings(settings).collection
    hits, trace = asyncio.run(
        recall_queries(
            settings,
            "space",
            "base",
            {"doc": (1, collection)},
            ["一", "二", "三", "四"],
            time.monotonic() + 2,
            lambda: None,
            Index(),
        )
    )
    assert peak == 4 and len(completed) == 8 and trace["queries"] == 4 and hits[0]["id"] == "doc"

    class Broken(Index):
        """模拟原生稀疏服务报错，不能回退掩盖故障。"""

        async def recall(self, *args):
            """稀疏失败时密集调用必须收到取消。"""
            if args[5]:
                raise RuntimeError("store_down")
            await asyncio.sleep(2)
            completed.append("unexpected")
            return []

    with pytest.raises(RuntimeError, match="store_down"):
        asyncio.run(
            recall_queries(
                settings,
                "space",
                "base",
                {"doc": (1, collection)},
                ["一", "二"],
                time.monotonic() + 3,
                lambda: None,
                Broken(),
            )
        )
    assert "unexpected" not in completed


def test_deadline_cancels_slow_http_body(monkeypatch):
    """绝对截止时间终止缓慢响应，不依赖单次读超时或遗留后台线程。"""
    cancelled = []

    class Slow(httpx.AsyncByteStream):
        """模拟永远不完成的外部 HTTP 响应。"""

        async def __aiter__(self):
            """等待取消后记录，证明底层请求确实终止。"""
            try:
                await asyncio.sleep(2)
                yield b"{}"
            finally:
                cancelled.append(True)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.rag.embeddings.httpx.AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Slow())),
            **kwargs,
        ),
    )
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        asyncio.run(bounded_post("https://fixture.test", {}, {}, started + 0.05, lambda: None))
    assert time.monotonic() - started < 0.5 and cancelled


def test_auto_merge_identity_version_and_revocation(client, storage):
    """合并只使用同版本真实父子关系，身份稳定并随版本变化，撤权后停止读取。"""
    identity, _, key, doc = uploaded(client, "Milvus 保存向量。" * 220)
    settings, engine = client.app.state.settings, client.app.state.database
    space, user = identity["user"]["id"], identity["user"]["id"]
    with Session(engine) as db:
        scope = document_scope(db, settings, space, user, key)
        children = list(
            db.scalars(select(Chunk).where(Chunk.document_id == doc["id"]).order_by(Chunk.ordinal))
        )
        parent = db.get(ParentChunk, children[0].parent_id)
        sources = materialize(
            db,
            settings,
            space,
            user,
            key,
            scope,
            [{"id": child.id, "rrf_score": 0.03} for child in children[:2]],
        )
        assert sources[0]["merged"] and sources[0]["content"] == parent.content
        assert len(sources[0]["child_ids"]) == 3
        again = materialize(
            db,
            settings,
            space,
            user,
            key,
            scope,
            [{"id": child.id, "rrf_score": 0.03} for child in children[:2]],
        )
        assert again[0]["evidence_id"] == sources[0]["evidence_id"]
        db.get(Document, doc["id"]).index_version = 2
        db.commit()
        assert (
            materialize(
                db, settings, space, user, key, scope, [{"id": children[0].id, "rrf_score": 0.03}]
            )
            == []
        )
        db.execute(
            __import__("sqlalchemy").update(User).where(User.id == user).values(active=False)
        )
        db.commit()
        with pytest.raises(ValueError, match="knowledge_permission_revoked"):
            materialize(db, settings, space, user, key, scope, [])


def test_evidence_pack_deduplication_and_budget():
    """去重后标签连续，正文总长度受限且截断显式标记。"""
    rows = [
        {"evidence_id": "1", "content": "a" * 600},
        {"evidence_id": "2", "content": "a" * 600},
        {"evidence_id": "3", "content": "b" * 600},
    ]
    packed = pack_evidence(rows, 700)
    assert len(packed) == 2 and packed[1]["label"] == "S2" and packed[1]["truncated"]
    assert sum(len(row["content"]) for row in packed) == 700


@pytest.mark.parametrize("characters", [700, 5000])
def test_grader_reads_packed_evidence_without_second_truncation(client, characters):
    """评判看到800字之后的答案，且不能越过与合成共用的上下文预算。"""
    settings = client.app.state.settings.model_copy(
        update={"model_provider": "openai_compatible", "rag_context_characters": characters}
    )
    answer = "核验答案在第800字之后。"
    first = "背景" * 500 + answer + "说明" * 400
    second = "其他资料" * 1000 + "预算外内容不得进入评判"
    received = []

    class Model:
        """只有收到答案原文才判为可回答。"""

        async def call(self, schema, purpose, payload, deadline):
            """捕获真实评判输入，检查预算、原文及证据身份。"""
            assert purpose == "grade"
            received.extend(payload["evidence"])
            visible = any(answer in source["content"] for source in received)
            return EvidenceGrade(
                relevance="strong" if visible else "weak",
                answerability="sufficient" if visible else "none",
                ambiguity="none",
                route="answer" if visible else "no_knowledge",
            )

    state = RagPipeline(
        client.app.state.database,
        settings,
        "space",
        "user",
        "base",
        lambda *args: None,
        lambda state: None,
        Model(),
    ).run(
        "",
        {
            "phase": "rerank",
            "question": "核验答案是什么？",
            "sources": [
                {"evidence_id": "E-first", "content": first},
                {"evidence_id": "E-second", "content": second},
            ],
            "rewrite_count": 0,
            "grade_count": 0,
            "clarification_count": 0,
            "trace": [],
        },
    )
    assert sum(len(source["content"]) for source in received) == characters
    assert "预算外内容不得进入评判" not in "".join(s["content"] for s in received)
    if characters == 5000:
        assert received == [
            {"evidence_id": "E-first", "content": first},
            {"evidence_id": "E-second", "content": second[: 5000 - len(first)]},
        ]
        assert state["route"] == "answer"
        assert [s["content"] for s in state["sources"]] == [s["content"] for s in received]
    else:
        assert received == [{"evidence_id": "E-first", "content": first[:700]}]
        assert state["route"] == "no_knowledge" and not state["sources"]


@pytest.mark.parametrize("method", ["step_back", "hyde"])
def test_one_rewrite_one_regrade_and_hyde_never_evidence(client, storage, monkeypatch, method):
    """每种策略都只重写一次和复评一次，假设文本不会成为回答证据。"""
    identity, _, key, _ = uploaded(client)
    settings = client.app.state.settings.model_copy(update={"model_provider": "openai_compatible"})
    calls = []

    class Model:
        """注入有意要求反复重写的评分器，验证程序限制优先于模型。"""

        async def call(self, schema, purpose, payload, deadline):
            """生成限定测试输出，记录阶段调用次数。"""
            calls.append(purpose)
            if purpose == "rewrite":
                return Rewrite(method=method, query="Milvus 假设文本不可成为证据")
            return EvidenceGrade(
                relevance="weak", answerability="partial", ambiguity="none", route="rewrite"
            )

    snapshots = []
    state = RagPipeline(
        client.app.state.database,
        settings,
        identity["user"]["id"],
        identity["user"]["id"],
        key,
        lambda *args: None,
        lambda state: snapshots.append(copy.deepcopy(state)),
        Model(),
    ).run("Milvus 保存向量")
    assert calls == ["grade", "rewrite", "grade"]
    assert state["rewrite_count"] == 1 and state["grade_count"] == 2 and state["sources"] == []
    assert next(row["method"] for row in state["trace"] if row["stage"] == "rewrite") == method
    assert all(
        "假设文本不可成为证据" not in source["content"]
        for saved in snapshots
        for source in saved["sources"]
    )


@pytest.mark.parametrize(
    ("relevance", "answerability", "ambiguity", "expected", "rewrites"),
    [
        ("strong", "sufficient", "none", "answer", 0),
        ("weak", "partial", "none", "no_knowledge", 1),
        ("none", "none", "none", "no_knowledge", 1),
        ("weak", "partial", "missing_slot", "clarify", 0),
        ("strong", "partial", "multiple_candidates", "clarify", 0),
    ],
)
def test_clarification_requires_question_ambiguity(
    client, storage, relevance, answerability, ambiguity, expected, rewrites
):
    """无歧义的错误澄清转为回答或有界检索，真正歧义仍交给用户。"""
    identity, _, key, _ = uploaded(client)
    settings = client.app.state.settings.model_copy(update={"model_provider": "openai_compatible"})

    class Model:
        """模拟评分器将证据不足误报为需要澄清。"""

        async def call(self, schema, purpose, payload, deadline):
            """持续返回澄清，验证程序保护和重写上限。"""
            if purpose == "rewrite":
                return Rewrite(method="step_back", query="Milvus 保存向量")
            return EvidenceGrade(
                relevance=relevance,
                answerability=answerability,
                ambiguity=ambiguity,
                route="clarify",
                hitl_prompt="请补充对象或版本。",
            )

    state = RagPipeline(
        client.app.state.database,
        settings,
        identity["user"]["id"],
        identity["user"]["id"],
        key,
        lambda *args: None,
        lambda state: None,
        Model(),
    ).run("Milvus 保存什么？")
    assert state["route"] == expected
    assert state["rewrite_count"] == rewrites
    assert state["grade_count"] == 1 + rewrites
    assert bool(state["sources"]) == (expected != "no_knowledge")
    assert (state["phase"] == "clarify") == (expected == "clarify")


def test_grader_rejects_unsupported_answer_and_unknown_route():
    """评分器不能通过不一致字段或未知路由绕过拒答与澄清。"""
    for data in [
        dict(relevance="weak", answerability="partial", ambiguity="none", route="answer"),
        dict(relevance="strong", answerability="sufficient", ambiguity="none", route="arbitrary"),
    ]:
        with pytest.raises(ValidationError):
            EvidenceGrade.model_validate(data)


@pytest.mark.parametrize("mode", ["empty", "unrelated", "rewrite_exhausted"])
def test_missing_knowledge_never_calls_answer_model(client, storage, monkeypatch, mode):
    """空库、无关资料和一次纠错耗尽均固定拒答，不能调用通用回答模型。"""
    if mode == "empty":
        _, headers, key = base(client)
    else:
        _, headers, key, _ = uploaded(client)

        def grade(question, sources):
            """模拟评分发现资料无关或持续证据不足。"""
            return EvidenceGrade(
                relevance="none" if mode == "unrelated" else "weak",
                answerability="none" if mode == "unrelated" else "partial",
                ambiguity="none",
                route="no_knowledge" if mode == "unrelated" else "rewrite",
            )

        monkeypatch.setattr("app.rag.pipeline.offline_grade", grade)

    def forbidden(*args):
        """没有支持证据时，任何回答供应商实例化都代表兜底泄漏。"""
        raise AssertionError("不得调用通用回答模型")

    task, _ = rag_task(client, headers, key, "你知道薛定谔的猫吗")
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(forbidden)
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        message = db.get(Message, task["id"])
        assert run.status == "succeeded" and run.error_code is None
        assert message.content == "没有相关知识" and not message.sources
        assert run.checkpoint["rag"]["route"] == "no_knowledge"
        assert run.checkpoint["rag"]["rewrite_count"] == (1 if mode == "rewrite_exhausted" else 0)


def test_empty_knowledge_skips_remote_rewrite(client, storage):
    """真实模型配置下空库直接拒答，不能用远端 HyDE 或通用知识填充证据。"""
    identity, _, key = base(client)

    class ForbiddenModel:
        """禁止空库路径产生任何真实模型调用。"""

        async def call(self, *args):
            """收到调用即失败，验证空检索不请求评分或改写模型。"""
            raise AssertionError("空库不应调用模型")

    state = RagPipeline(
        client.app.state.database,
        client.app.state.settings.model_copy(update={"model_provider": "openai_compatible"}),
        identity["user"]["id"],
        identity["user"]["id"],
        key,
        lambda *args: None,
        lambda state: None,
        ForbiddenModel(),
    ).run("你知道薛定谔的猫吗")
    assert state["route"] == "no_knowledge" and state["phase"] == "done"
    assert not state["sources"] and state["rewrite_count"] == 0


def test_execution_unknown_error_logs_safe_location(client, storage, monkeypatch, caplog):
    """真实执行边界遇到未知异常会保存失败终态，并留下不含资料正文的定位信息。"""
    _, headers, key, _ = uploaded(client)
    task, _ = rag_task(client, headers, key, "Milvus 保存什么")

    def broken(*args):
        """模拟意外编排故障，异常正文故意包含敏感内容标记。"""
        raise ValueError("private-user-evidence")

    monkeypatch.setattr("app.rag.pipeline.RagPipeline.run", broken)
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute()
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        assert run.status == "failed" and run.error_code == "execution_error"
    assert task["id"] in caplog.text and "exception_type=ValueError" in caplog.text
    assert "private-user-evidence" not in caplog.text


def test_rerank_bounds_breaker_and_permissions(monkeypatch):
    """限定输入后请求，达到失败阈值后熔断；权限撤销不被回退吞掉。"""
    _breakers.clear()
    settings = Settings(
        _env_file=None,
        app_env="test",
        rag_rerank_url="https://fixture.test/rerank",
        rag_rerank_max_characters=700,
        rag_rerank_failure_threshold=2,
    )
    observed = []

    async def fail(url, payload, headers, deadline, guard):
        """记录真实请求边界并模拟超时。"""
        observed.append(payload)
        raise TimeoutError()

    monkeypatch.setattr("app.rag.rerank.bounded_post", fail)
    rows = [
        {"content": "a" * 1000, "evidence_id": "1"},
        {"content": "b" * 1000, "evidence_id": "2"},
    ]
    reranker = Reranker(settings)
    assert (
        asyncio.run(reranker.run("查询", rows, time.monotonic() + 1, lambda: None))[1] == "fallback"
    )
    assert (
        asyncio.run(reranker.run("查询", rows, time.monotonic() + 1, lambda: None))[1] == "fallback"
    )
    assert (
        asyncio.run(reranker.run("查询", rows, time.monotonic() + 1, lambda: None))[1]
        == "circuit_open"
    )
    assert len(observed) == 2 and len(observed[0]["documents"][0]) == 700

    def denied():
        """模拟外层取消与权限失败，必须直接终止流程。"""
        from app.providers.client import ProviderFailure

        raise ProviderFailure("permission_revoked")

    with pytest.raises(Exception, match="permission_revoked"):
        asyncio.run(reranker.run("查询", rows, time.monotonic() + 1, denied))


def test_hitl_same_run_idempotency_and_budget(client, storage):
    """暂停后用同一任务恢复；幂等重试不重复消息，预算只在最终结束结算。"""
    _, headers, key, _ = uploaded(client)
    task, headers = rag_task(client, headers, key, "它的 Milvus 用途是什么？")
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute()
    paused = client.get(f"/api/v1/runs/{task['id']}").json()
    assert paused["status"] == "waiting_input" and paused["clarification"]["prompt"]
    body = {"answer": "Milvus 保存向量", "state_version": paused["state_version"]}
    headers["Idempotency-Key"] = str(uuid4())
    resumed = client.post(f"/api/v1/runs/{task['id']}/clarify", json=body, headers=headers)
    assert resumed.status_code == 202 and resumed.json()["id"] == task["id"]
    assert (
        client.post(f"/api/v1/runs/{task['id']}/clarify", json=body, headers=headers).status_code
        == 202
    )
    assert (
        client.post(
            f"/api/v1/runs/{task['id']}/clarify",
            json={**body, "answer": "不同补充"},
            headers=headers,
        ).status_code
        == 409
    )
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute()
    final = client.get(f"/api/v1/runs/{task['id']}").json()
    assert final["status"] == "succeeded"
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        assert run.snapshot["sources"][0]["evidence_id"].startswith("E-")
        assert db.get(Budget, "user:" + run.owner_id).reserved == 0


@pytest.mark.parametrize("failure", ["deleted", "expired", "foreign", "stale"])
def test_hitl_rejects_stale_deleted_expired_and_foreign(client, storage, failure):
    """资料删除、澄清过期、其他用户或旧状态版本均不可恢复。"""
    _, headers, key, doc = uploaded(client)
    task, headers = rag_task(client, headers, key, "它的 Milvus 用途？")
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute()
    paused = client.get(f"/api/v1/runs/{task['id']}").json()
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        if failure == "deleted":
            db.get(Document, doc["id"]).status = "deleting"
        if failure == "expired":
            run.snapshot = {
                **run.snapshot,
                "rag_clarification": {"expires_at": (now() - timedelta(seconds=1)).isoformat()},
            }
        db.commit()
    if failure == "foreign":
        foreign = login(client, "member@example.test")
        headers = {**ORIGIN, "X-CSRF-Token": foreign["csrf_token"]}
    headers["Idempotency-Key"] = str(uuid4())
    response = client.post(
        f"/api/v1/runs/{task['id']}/clarify",
        json={
            "answer": "Milvus",
            "state_version": paused["state_version"] - (1 if failure == "stale" else 0),
        },
        headers=headers,
    )
    assert (
        response.status_code
        == {"deleted": 409, "expired": 410, "foreign": 404, "stale": 409}[failure]
    )
    if failure == "expired":
        sweep(
            client.app.state.database,
            client.app.state.settings,
            lambda *args: None,
            lambda *args: None,
        )
        with Session(client.app.state.database) as db:
            assert db.get(Run, task["id"]).error_code == "rag_clarification_expired"


def test_structured_model_protocol_and_schema(monkeypatch):
    """真实 HTTP 协议输出经现有结束标记和严格 Schema 校验后才用于路由。"""
    data = {
        "relevance": "strong",
        "answerability": "sufficient",
        "ambiguity": "none",
        "route": "answer",
    }
    frame = (
        "data: "
        + json.dumps(
            {"choices": [{"delta": {"content": json.dumps(data)}, "finish_reason": "stop"}]}
        )
        + "\n\ndata: [DONE]\n\n"
    )
    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.rag.model.httpx.AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=frame)), **kwargs
        ),
    )
    settings = Settings(
        _env_file=None,
        app_env="test",
        model_provider="openai_compatible",
        model_base_url="https://fixture.test/v1",
        model_api_key="test",
    )
    updates = []
    result = asyncio.run(
        StructuredModel(settings, lambda *args: updates.append(args)).call(
            EvidenceGrade, "grade", {}, time.monotonic() + 2
        )
    )
    assert result.route == "answer" and any(row[2] > 0 for row in updates)


def test_rag_checkpoint_restart_skips_finished_retrieval(client, storage, monkeypatch):
    """评分前宕机后从该节点恢复，已完成的混合检索不重复执行。"""
    from app.rag import pipeline
    from test_reliability import expire_lease, recover

    _, headers, key, _ = uploaded(client)
    task, headers = rag_task(client, headers, key, "Milvus 保存向量")
    original = pipeline.offline_grade

    def crash(*args):
        """在持久检查点之后模拟进程退出，而非普通可捕获业务失败。"""
        raise SystemExit("评分前宕机")

    monkeypatch.setattr(pipeline, "offline_grade", crash)
    with pytest.raises(SystemExit):
        Execution(client.app.state.database, client.app.state.settings, task["id"]).execute()
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        assert run.checkpoint["rag"]["phase"] == "grade"
    monkeypatch.setattr(pipeline, "offline_grade", original)

    async def repeated(*args):
        """恢复已完成节点时不允许重新调用召回。"""
        raise AssertionError("不应重复检索")

    monkeypatch.setattr(pipeline, "recall_queries", repeated)
    expire_lease(client, task["id"])
    recover(client)
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute()
    assert client.get(f"/api/v1/runs/{task['id']}").json()["status"] == "succeeded"


def test_rerank_success_and_malformed_results(monkeypatch):
    """有效结果改变顺序，重复或越界索引明确回退到原顺序。"""
    _breakers.clear()
    settings = Settings(
        _env_file=None, app_env="test", rag_rerank_url="https://success.fixture/rerank"
    )
    response = {
        "results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1}]
    }

    async def reply(*args):
        """提供受控重排响应，验证不信任供应商索引与数值。"""
        return response

    monkeypatch.setattr("app.rag.rerank.bounded_post", reply)
    rows = [{"content": "甲", "evidence_id": "1"}, {"content": "乙", "evidence_id": "2"}]
    ranked, status = asyncio.run(
        Reranker(settings).run("查询", rows, time.monotonic() + 2, lambda: None)
    )
    assert status == "applied" and ranked[0]["evidence_id"] == "2"
    response["results"] = [
        {"index": 0, "relevance_score": 0.9},
        {"index": 0, "relevance_score": 0.8},
    ]
    ranked, status = asyncio.run(
        Reranker(settings).run("查询", rows, time.monotonic() + 2, lambda: None)
    )
    assert status == "fallback" and ranked == rows


def test_source_preview_checks_evidence_identity_and_parent(client, storage):
    """合并证据预览返回父级原文，错误证据身份和伪造父级都拒绝访问。"""
    identity, _, key, _ = uploaded(client, "Milvus 保存向量，资料版本由 PostgreSQL 保存。" * 100)
    with Session(client.app.state.database) as db:
        sources = retrieve(
            db,
            client.app.state.settings,
            identity["user"]["id"],
            identity["user"]["id"],
            key,
            "Milvus 保存向量",
        )
    merged = next(row for row in sources if row.get("merged"))
    query = {
        "parent_id": merged["parent_id"],
        "evidence_id": merged["evidence_id"],
        "index_version": merged["index_version"],
    }
    url = f"/api/v1/sources/{merged['chunk_id']}"
    assert client.get(url, params=query).json()["content"] == merged["content"]
    assert client.get(url, params={**query, "evidence_id": "E-" + "0" * 24}).status_code == 404
    assert client.get(url, params={**query, "parent_id": str(uuid4())}).status_code == 404


def test_controlled_rebuild_failure_keeps_published_index(client, storage, monkeypatch):
    """候选写入失败不发布集合指针或父子关系，旧 ready 资料仍可检索。"""
    from app.rag.rebuild import rebuild_document

    _, _, _, doc = uploaded(client)
    settings, engine = client.app.state.settings, client.app.state.database
    legacy = Embeddings(settings).legacy_collection
    with Session(engine) as db:
        db.get(Document, doc["id"]).collection_name = legacy
        for chunk in db.scalars(select(Chunk).where(Chunk.document_id == doc["id"])):
            chunk.parent_id = None
        db.flush()
        db.execute(delete(ParentChunk).where(ParentChunk.document_id == doc["id"]))
        db.commit()

    class Store:
        """模拟读取旧索引成功、写入候选失败的跨存储故障。"""

        def __init__(self, settings):
            """测试不访问真实向量服务。"""
            pass

        def ensure(self, embedding):
            """候选集合已存在，专门验证发布前的写入失败。"""
            pass

        def request(self, operation, payload):
            """返回旧版本向量条目的完整身份。"""
            return {"data": list(storage[1].values())}

        def upsert(self, collection, rows):
            """模拟 Milvus 在写入候选期间不可用。"""
            raise RuntimeError("candidate_write_failed")

    monkeypatch.setattr("app.rag.rebuild.VectorIndex", Store)
    with pytest.raises(RuntimeError, match="candidate_write_failed"):
        rebuild_document(engine, settings, doc["id"])
    with Session(engine) as db:
        row = db.get(Document, doc["id"])
        assert row.status == "ready" and row.collection_name == legacy
        assert (
            list(db.scalars(select(ParentChunk).where(ParentChunk.document_id == doc["id"]))) == []
        )
        assert all(
            chunk.parent_id is None
            for chunk in db.scalars(select(Chunk).where(Chunk.document_id == doc["id"]))
        )

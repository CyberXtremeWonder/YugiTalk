"""七步增强 RAG：有界规划、并行混合检索、重排、评判、一次纠错与可恢复合成。"""

import asyncio
import re
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.observability.telemetry import operation_span
from app.observability.traces import trace_step
from app.providers.client import ProviderFailure
from app.rag.contracts import EvidenceGrade, QueryPlan, Rewrite
from app.rag.graph import fuse_graph
from app.rag.model import StructuredModel
from app.rag.rerank import Reranker
from app.rag.retrieval import document_scope, materialize, pack_evidence, recall_queries

Update = Callable[[str | None, dict[str, object] | None, int], None]
Save = Callable[[dict[str, Any]], None]


def local_plan(question: str) -> QueryPlan:
    """短单事实问题直接查询；明显多意图按连接词拆分，长问题生成有界检索视角。"""
    complex_query = len(question) > 120 or any(
        word in question for word in ("比较", "对比", "分别", "以及", "同时", "区别")
    )
    if not complex_query:
        return QueryPlan(complexity="simple")
    parts = list(
        dict.fromkeys(
            part.strip(" ，。？?")
            for part in re.split(r"[；;\n]|以及|同时|分别|与|和", question)
            if part.strip(" ，。？?")
        )
    )
    if len(parts) < 2:
        parts = [question[:850] + "：定义与适用条件", question[:850] + "：差异、限制与实现方式"]
    return QueryPlan(complexity="complex", sub_questions=[part[:1000] for part in parts[:4]])


def offline_grade(question: str, sources: list[dict[str, Any]]) -> EvidenceGrade:
    """空证据直接拒答；非空证据的可复现评分规则仅供 Mock 使用。"""
    if not sources:
        return EvidenceGrade(
            relevance="none", answerability="none", ambiguity="none", route="no_knowledge"
        )
    if any(word in question for word in ("它的", "这个的", "那个的")) and "补充：" not in question:
        return EvidenceGrade(
            relevance="weak",
            answerability="partial",
            ambiguity="missing_slot",
            route="clarify",
            hitl_prompt="请补充你指的是哪个对象或版本。",
        )
    text = "".join(source["content"] for source in sources).lower()
    tokens = re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2}", question.lower())
    if not any(token in text for token in tokens):
        return EvidenceGrade(
            relevance="weak", answerability="partial", ambiguity="none", route="rewrite"
        )
    return EvidenceGrade(
        relevance="strong", answerability="sufficient", ambiguity="none", route="answer"
    )


class RagPipeline:
    """持久化每个完成节点，重写和复评次数跨重启及人工澄清保持有界。"""

    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        workspace: str,
        user: str,
        base: str,
        update: Update,
        save: Save,
        model: StructuredModel | None = None,
    ) -> None:
        """注入授权范围、任务预算与保存接口，不在并行分支共享会话或事务。"""
        self.engine, self.settings = engine, settings
        self.workspace, self.user, self.base = workspace, user, base
        self.update, self.save = update, save
        self.model = model or StructuredModel(settings, update)

    def run(self, question: str, resume: dict[str, Any] | None = None) -> dict[str, Any]:
        """在同步 Worker 中进入异步编排，超时和异常保持公开安全错误码。"""
        try:
            with operation_span("rag.execute"):
                return asyncio.run(self.execute(question, resume))
        except TimeoutError:
            raise ProviderFailure("rag_deadline") from None

    async def execute(self, question: str, resume: dict[str, Any] | None) -> dict[str, Any]:
        """完成七步流程；只有授权的 SQL 原文进入答案，HyDE 查询从不进入证据。"""
        state: dict[str, Any] = (
            dict(resume)
            if resume
            else {
                "phase": "plan",
                "question": question,
                "queries": [],
                "sources": [],
                "rewrite_count": 0,
                "grade_count": 0,
                "clarification_count": 0,
                "trace": [],
            }
        )
        deadline = time.monotonic() + self.settings.rag_timeout_seconds
        # 所有检索/模型分支共用一次执行的截止时间，不能每走一步就重置完整超时额度。

        def guard() -> None:
            """复核任务租约、取消、权限和保守额度。"""
            self.update(None, None, 0)

        async with asyncio.timeout(self.settings.rag_timeout_seconds):
            while state["phase"] != "done":
                guard()
                phase = state["phase"]
                if phase == "clarify":
                    return state
                self.update("tool_status", {"stage": "rag_" + phase}, 0)
                with trace_step(
                    "retrieval" if phase == "retrieve" else "rag", "rag_" + phase
                ) as phase_step:
                    if phase == "plan":
                        plan = local_plan(state["question"])
                        # 简单问题直接检索；复杂问题且配置了快速模型时，才额外请求模型拆题。
                        if (
                            plan.complexity == "complex"
                            and self.settings.rag_fast_model
                            and self.settings.model_provider != "mock"
                        ):
                            plan = await self.model.call(
                                QueryPlan, "plan", {"question": state["question"][:2000]}, deadline
                            )
                        state["complexity"] = plan.complexity
                        state["trace"].append({"stage": "plan", "complexity": plan.complexity})
                        state["queries"] = plan.sub_questions or [state["question"][:2000]]
                        state["phase"] = "retrieve"
                    elif phase == "retrieve":
                        with Session(self.engine) as db:
                            scope = document_scope(
                                db, self.settings, self.workspace, self.user, self.base
                            )
                        hits, meta = await recall_queries(
                            self.settings,
                            self.workspace,
                            self.base,
                            scope,
                            state["queries"],
                            deadline,
                            guard,
                        )
                        guard()
                        with Session(self.engine) as db:
                            current_scope = document_scope(
                                # 外部召回期间文档可能更新，图谱只使用前后版本一致的授权文档。
                                db, self.settings, self.workspace, self.user, self.base
                            )
                            graph_scope = {
                                key: version for key, version in scope.items()
                                if current_scope.get(key) == version
                            }
                            hits, meta["graph_candidates"] = fuse_graph(
                                db, self.settings, graph_scope, state["question"], hits
                            )
                            state["sources"] = materialize(
                                db, self.settings, self.workspace, self.user, self.base, scope, hits
                            )
                        state["trace"].append(
                            {
                                "stage": "retrieval",
                                **meta,
                                "merged": sum(
                                    bool(source.get("merged")) for source in state["sources"]
                                ),
                            }
                        )
                        state["phase"] = "rerank"
                    elif phase == "rerank":
                        state["sources"], status = await Reranker(self.settings).run(
                            state["question"], state["sources"], deadline, guard
                        )
                        state["trace"].append({"stage": "rerank", "status": status})
                        self.update("tool_status", {"stage": "rag_rerank", "status": status}, 0)
                        state["sources"] = pack_evidence(
                            # 重排后按优先级装入上下文预算，后面的评判与回答使用同一份证据包。
                            state["sources"], self.settings.rag_context_characters
                        )
                        state["phase"] = "grade"
                    elif phase == "grade":
                        if state["grade_count"] >= 2:
                            raise ProviderFailure("rag_grade_limit")
                        if self.settings.model_provider == "mock" or not state["sources"]:
                            grade = offline_grade(state["question"], state["sources"])
                        else:
                            grade = await self.model.call(
                                EvidenceGrade,
                                "grade",
                                {
                                    "question": state["question"][:2000],
                                    "evidence": [
                                        {
                                            "evidence_id": source["evidence_id"],
                                            "content": source["content"],
                                        }
                                        for source in state["sources"]
                                    ],
                                },
                                deadline,
                            )
                        state["grade_count"] += 1
                        state["grade"] = grade.model_dump()
                        route = grade.route
                        # 模型建议还要经过程序约束：无证据不能回答，有歧义优先澄清。
                        if not state["sources"] and route == "answer":
                            route = "rewrite"
                        elif grade.ambiguity != "none":
                            route = "clarify"
                        elif route == "clarify":
                            route = (
                                "answer"
                                if grade.relevance == "strong"
                                and grade.answerability == "sufficient"
                                else "rewrite"
                            )
                        # 强相关的部分证据已经可以支持有界回答，不再强制扩题、改写和清空来源。
                        if (
                            state["sources"]
                            and grade.relevance == "strong"
                            and grade.answerability == "partial"
                            and grade.ambiguity == "none"
                        ):
                            route = "partial_answer"
                        if route == "rewrite" and (
                            # 计数保存在检查点里，重启或补充信息也不能绕过一次改写、两次评判限制。
                            state["rewrite_count"] >= 1 or state["grade_count"] >= 2
                        ):
                            route = "no_knowledge"
                        if route == "clarify" and (
                            state["clarification_count"] >= 1 or state["grade_count"] >= 2
                        ):
                            route = "no_knowledge"
                        state["route"] = route
                        state["trace"].append(
                            {
                                "stage": "grade",
                                "relevance": grade.relevance,
                                "answerability": grade.answerability,
                                "ambiguity": grade.ambiguity,
                                "route": route,
                                "mode": "offline_rules"
                                if self.settings.model_provider == "mock"
                                else "model",
                            }
                        )
                        state["phase"] = {
                            "answer": "synthesis",
                            "partial_answer": "synthesis",
                            "rewrite": "rewrite",
                            "clarify": "clarify",
                            "no_knowledge": "synthesis",
                        }[route]
                    elif phase == "rewrite":
                        if state["rewrite_count"] >= 1:
                            raise ProviderFailure("rag_rewrite_limit")
                        rewrite = (
                            Rewrite(
                                method="step_back",
                                query="相关背景、概念和适用条件：" + state["question"][:1000],
                            )
                            if self.settings.model_provider == "mock"
                            else await self.model.call(
                                Rewrite,
                                "rewrite",
                                {"question": state["question"][:2000], "grade": state["grade"]},
                                deadline,
                            )
                        )
                        state["rewrite_count"] += 1
                        state["queries"] = [rewrite.query]
                        # 改写只替换检索词，原问题仍保留用于评判；假设答案不能成为真实证据。
                        state["trace"].append({"stage": "rewrite", "method": rewrite.method})
                        state["phase"] = "retrieve"
                    elif phase == "synthesis":
                        # 合成只输出证据包给现有流式回答节点；无证据和澄清耗尽由程序说明限制。
                        state["sources"] = (
                            pack_evidence(state["sources"], self.settings.rag_context_characters)
                            if state["route"] in {"answer", "partial_answer"}
                            else []
                        )
                        state["phase"] = "done"
                    else:
                        raise ProviderFailure("rag_checkpoint_invalid")
                    if state["trace"]:
                        phase_step.details.update(state["trace"][-1])
                guard()
                self.save(state)
        return state

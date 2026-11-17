"""本机 Docker 中隔离比较候选上限，复用鉴权和实际混合检索实现。"""

import hashlib
import json
import statistics
import subprocess
import sys
from collections import Counter
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "outputs/acg_simpleqa_small_20261029"
OUT = ROOT / "outputs/rag-candidates-20261030"


def worker():
    """仅修改本进程 Settings 副本，不更新全局服务或数据库配置。"""
    import re
    import time

    import httpx
    from app.accounts.access import membership
    from app.core.config import load_settings
    from app.observability.telemetry import Telemetry, current_trace_id
    from app.rag.indexing import retrieve
    from app.rag.models import Document, KnowledgeBase
    from app.tasks.database import database
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    payload = json.load(sys.stdin)
    private = payload.pop("private")
    with httpx.Client(base_url="http://127.0.0.1:8000", trust_env=False, timeout=20) as client:
        response = client.post("/api/v1/auth/login", json={
            "email": private["RAG_TEST_ACCOUNT"], "password": private["RAG_TEST_PASSWORD"],
        }, headers={"Origin": private["RAG_TEST_ORIGIN"]})
        response.raise_for_status()
        user = response.json()["user"]["id"]
    engine, _ = database.get()
    settings = load_settings().model_copy(update={"otel_sample_ratio": 1.0})
    telemetry = Telemetry(settings, "rag-comparison")
    normalize = lambda text: re.sub(r"\s+", "", text).casefold()
    with Session(engine) as db:
        base = db.get(KnowledgeBase, payload["base"])
        assert base and not base.deleted
        workspace = base.account_id
        membership(db, workspace, user)

        def snapshot():
            return sorted((d.id, d.status, d.index_version, d.content_hash, d.embedding_version)
                          for d in db.scalars(select(Document).where(
                              Document.knowledge_base_id == base.id)))

        before = snapshot()
        assert before and all(d[1] == "ready" for d in before)
        print(json.dumps({"kind": "manifest", "documents": before,
                          "config": {k: getattr(settings, k) for k in (
                              "rag_context_characters", "rag_merge_ratio", "rag_graph_enabled",
                              "retrieval_min_score", "embedding_model", "embedding_revision")}}),
              flush=True)

        def run(case, limit, repetition):
            config = settings.model_copy(update={"rag_candidate_limit": limit})
            with telemetry.span("experiment.rag-candidates") as span:
                for key, value in {"experiment": "rag-candidates-20261030", "candidate_limit": limit,
                                   "case": case["original_index"], "repetition": repetition}.items():
                    span.set_attribute("langfuse.observation.metadata." + key, value)
                started = time.perf_counter()
                sources = retrieve(db, config, workspace, user, base.id, case["question"])
                seconds = time.perf_counter() - started
                covered = any(normalize(case["answer"]) in normalize(s["content"]) for s in sources)
                span.set_attribute("langfuse.observation.metadata.answer_text_covered", covered)
                return {"kind": "result", "case": case["original_index"], "category": case["category"],
                        "candidate_limit": limit, "repetition": repetition, "seconds": seconds,
                        "answer_text_covered": covered, "trace_id": current_trace_id(),
                        "evidence": [{k: s[k] for k in ("evidence_id", "document_id", "index_version")}
                                     for s in sources], "characters": sum(len(s["content"]) for s in sources)}
        try:
            for limit in (12, 24):
                run(payload["cases"][0], limit, -1)
            for repetition in range(3):
                for index, case in enumerate(payload["cases"]):
                    order = (12, 24) if (index + repetition) % 2 == 0 else (24, 12)
                    for limit in order:
                        print(json.dumps(run(case, limit, repetition)), flush=True)
            db.expire_all()
            assert snapshot() == before, "实验中索引发生变化，结果不可作为受控对照"
            print(json.dumps({"kind": "complete", "index_unchanged": True}), flush=True)
        finally:
            telemetry.shutdown()


def main():
    cases = []
    counts = Counter()
    for line in (SOURCE / "acg_simpleqa.jsonl").read_text(encoding="utf-8-sig").splitlines():
        case = json.loads(line)
        if counts[case["category"]] < 4:
            cases.append(case)
            counts[case["category"]] += 1
    assert len(cases) == 20 and len(counts) == 5
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    payload = {"private": dict(dotenv_values(ROOT / "backend/.env.rag-test")),
               "base": json.loads((SOURCE / "import.json").read_text())["knowledge_base_id"],
               "cases": cases}
    with (OUT / "results.jsonl").open("w", encoding="utf-8") as output:
        subprocess.run(["docker", "compose", "exec", "-T", "api", "python", "-c",
                        Path(__file__).read_text(encoding="utf-8").replace("from dotenv import dotenv_values", "")
                        .replace("ROOT = Path(__file__).resolve().parents[1]", "ROOT = Path('/app')")
                        .replace("    main()", "    worker()")], input=json.dumps(payload),
                       text=True, encoding="utf-8", stdout=output, cwd=ROOT, check=True)
    rows = [json.loads(line) for line in (OUT / "results.jsonl").read_text().splitlines()]
    assert rows[-1]["kind"] == "complete"
    summary = {}
    for limit in (12, 24):
        results = [r for r in rows if r["kind"] == "result" and r["candidate_limit"] == limit]
        seconds = sorted(r["seconds"] for r in results)
        summary[limit] = {"requests": len(results), "coverage": statistics.mean(
            r["answer_text_covered"] for r in results), "median_seconds": statistics.median(seconds),
            "p95_seconds": seconds[56], "mean_evidence": statistics.mean(len(r["evidence"]) for r in results)}
    report = {"summary": summary, "cases_sha256": hashlib.sha256((OUT / "cases.json").read_bytes()).hexdigest(),
              "manifest": rows[0], "limits": "20 questions, 3 repeats, answer substring proxy; no generation or judge"}
    (OUT / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()

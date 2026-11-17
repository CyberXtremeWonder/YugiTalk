"""Compare isolated pgvector/Milvus retrieval with frozen project inputs; no generation API."""

import argparse
import hashlib
import importlib.util
import json
import random
import sys
import time
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/vector-db-compare-20261108"
sys.path.insert(0, str(OUT / "deps"))
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np
import psycopg
from pymilvus import DataType, Function, FunctionType, MilvusClient, connections, utility
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.entities import Base
from app.identity.models import User
from app.rag.embeddings import Embeddings
from app.rag.hierarchy import attach_parents
from app.rag.models import Chunk, Document, KnowledgeBase, ParentChunk
from app.rag.retrieval import document_scope, materialize, pack_evidence, rrf


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--reset-isolated", action="store_true", help="Reset only benchmark tables/collections at the fixed isolated endpoints")
    args = parser.parse_args()
    if (OUT / "summary.json").exists():
        raise ValueError("Completed output already exists; preserve the original experiment")
    spec = importlib.util.spec_from_file_location("scorer", ROOT / "scripts/evaluate-knowledge-retrieval.py")
    scorer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scorer)
    source = ROOT / "outputs/chunk-profiles-20261027/markdown_sections-documents.json"
    dataset = ROOT / "outputs/miracl-zh-dev/cases.json"
    cache = ROOT / "outputs/token-chunking-20261026/vectors"
    docs, cases = read(source), scorer.load_cases(dataset)
    parts = [p for doc in docs for p in doc["parts"]]
    vectors = np.stack([np.load(cache / (p["vector_key"] + ".npy")) for p in parts])
    queries = np.stack([np.load(cache / (hashlib.sha256(c["question"].encode()).hexdigest() + ".npy")) for c in cases])
    assert vectors.shape == (3817, 1024) and queries.shape == (393, 1024)
    assert np.isfinite(vectors).all() and np.isfinite(queries).all()
    assert all(hashlib.sha256(p["content"].encode()).hexdigest() == p["vector_key"] for p in parts)
    owner = str(uuid5(NAMESPACE_URL, "vector-store-compare-owner"))
    base = str(uuid5(NAMESPACE_URL, "vector-store-compare-base"))
    modes = ("pg_exact", "milvus_flat", "pg_hnsw", "milvus_hnsw")
    manifest = {
        "scope": "RAG retrieval only; MIRACL zh dev judged passage pool, not full corpus or answer accuracy",
        "documents": len(docs), "chunks": len(parts), "questions": len(cases),
        "source_sha256": sha(source), "cases_sha256": sha(dataset),
        "vectors_sha256": hashlib.sha256(vectors.tobytes()).hexdigest(),
        "queries_sha256": hashlib.sha256(queries.tobytes()).hexdigest(),
        "model": read(cache / "model.json"), "seed": 20261108,
        "hnsw": {"M": 16, "efConstruction": 64, "efSearch": 100},
        "main_consistency": "Milvus Session after this same client inserted/flushed all rows; immutable corpus; PostgreSQL READ COMMITTED",
        "consistency_sensitivity": "Additional Strong vs Session HNSW measurements on 50 seeded queries, two rounds",
        "top_candidates": 24, "top_sources": 5, "context_characters": 5000,
        "dense_threshold": 0.35, "rrf_k": 60, "repetitions": args.repetitions,
        "hybrid_control": "Same Milvus native jieba BM25 candidates reused for every arm; NOT standalone PostgreSQL BM25",
        "latency": "Warm single-client native protocol roundtrip incl vector serialization; excludes embeddings, lexical, SQL materialization, generation",
        "source_hashes": {p: sha(ROOT / p) for p in (
            "scripts/benchmark-vector-stores.py", "backend/app/rag/retrieval.py",
            "backend/app/rag/hierarchy.py", "scripts/evaluate-knowledge-retrieval.py")},
    }
    save("manifest.json", manifest)
    client = MilvusClient(uri="http://127.0.0.1:49530", timeout=120)
    connections.connect(alias="segment_verification", host="127.0.0.1", port="49530")
    conn = psycopg.connect("host=127.0.0.1 port=45432 dbname=vector_eval user=vector_eval password=isolated-evaluation-only", autocommit=True)
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.execute("SET jit=off")
    if args.reset_isolated:
        for mode in ("pg_exact", "pg_hnsw"):
            conn.execute(f"DROP TABLE IF EXISTS {mode}")
        for mode in ("milvus_flat", "milvus_hnsw"):
            if client.has_collection(mode):
                client.drop_collection(mode)
    manifest["versions"] = {"milvus": client.get_server_version(), "postgres": conn.execute("SHOW server_version").fetchone()[0], "pgvector": conn.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()[0]}
    timings = {}
    print("Loading identical vectors into fresh tables and collections", flush=True)
    for mode in ("pg_exact", "pg_hnsw"):
        started = time.perf_counter()
        conn.execute(f"CREATE TABLE {mode} (id text PRIMARY KEY, base_id text, version int, content text, embedding vector(1024))")
        with conn.cursor().copy(f"COPY {mode} FROM STDIN") as copy:
            for p, v in zip(parts, vectors, strict=True):
                copy.write_row((p["id"], base, 1, p["content"], json.dumps(v.tolist())))
        if mode == "pg_hnsw":
            conn.execute("CREATE INDEX pg_hnsw_embedding ON pg_hnsw USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64)")
        conn.execute(f"ANALYZE {mode}")
        timings[mode] = time.perf_counter() - started
        assert conn.execute(f"SELECT count(*) FROM {mode}").fetchone()[0] == len(parts)
    for mode, kind in (("milvus_flat", "FLAT"), ("milvus_hnsw", "HNSW")):
        started = time.perf_counter()
        schema = client.create_schema(auto_id=False, enable_dynamic_field=True)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=36)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=1024)
        schema.add_field("text", DataType.VARCHAR, max_length=8192, enable_analyzer=True, analyzer_params={"tokenizer": "jieba"})
        schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_function(Function(name="bm25", input_field_names=["text"], output_field_names=["sparse"], function_type=FunctionType.BM25))
        index = client.prepare_index_params()
        index.add_index("vector", index_name="dense", index_type=kind, metric_type="COSINE", params={"M": 16, "efConstruction": 64} if kind == "HNSW" else {})
        index.add_index("sparse", index_name="bm25", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25", params={"inverted_index_algo": "DAAT_MAXSCORE"})
        client.create_collection(mode, schema=schema, index_params=index, consistency_level="Strong")
        for begin in range(0, len(parts), 128):
            client.insert(mode, [{"id": p["id"], "text": p["content"], "vector": v.tolist(), "base_id": base, "version": 1} for p, v in zip(parts[begin:begin + 128], vectors[begin:begin + 128], strict=True)])
        client.flush(mode)
        deadline = time.monotonic() + 180
        while True:
            state = client.describe_index(mode, "dense")
            if int(state.get("indexed_rows", 0)) == len(parts):
                break
            if time.monotonic() > deadline:
                raise RuntimeError(f"Index not fully built: {state}")
            time.sleep(1)
        # Force queries onto sealed, indexed data rather than still-loaded growing segments.
        client.release_collection(mode)
        client.load_collection(mode)
        segments = utility.get_query_segment_info(mode, using="segment_verification")
        assert sum(s.num_rows for s in segments) == len(parts)
        assert all(s.state == 3 and s.indexID > 0 for s in segments)
        manifest[mode + "_loaded_segments"] = [str(s) for s in segments]
        timings[mode] = time.perf_counter() - started
        manifest[mode + "_index"] = state
        assert int(client.get_collection_stats(mode)["row_count"]) == len(parts)
    save("setup-seconds.json", timings)
    conn.execute("SET hnsw.ef_search=100")
    conn.execute("SET hnsw.iterative_scan='strict_order'")
    conn.execute("SET enable_seqscan=off")
    query_sql = "SELECT id, 1-(embedding <=> %(vector)s::vector) AS similarity FROM {} WHERE base_id=%(base)s AND version=1 ORDER BY embedding <=> %(vector)s::vector LIMIT 24"
    query_texts = [json.dumps(q.tolist()) for q in queries]
    plan = conn.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + query_sql.format("pg_hnsw"), {"vector": query_texts[0], "base": base}).fetchone()[0]
    assert "pg_hnsw_embedding" in json.dumps(plan)
    save("pg-hnsw-query-plan.json", plan)

    def search(mode, number):
        if mode.startswith("pg"):
            text = json.dumps(queries[number].tolist())
            return [{"id": key, "distance": float(score)} for key, score in conn.execute(query_sql.format(mode), {"vector": text, "base": base}).fetchall()]
        rows = client.search(mode, data=[queries[number].tolist()], anns_field="vector", filter=f'base_id == "{base}" and version == 1', limit=24, search_params={"metric_type": "COSINE", "params": {"ef": 100} if mode.endswith("hnsw") else {}}, consistency_level="Session")[0]
        return [{"id": r["id"], "distance": float(r["distance"])} for r in rows]

    # All backends receive the exact same float32 values. Exact cosine oracle is float64.
    a, b = vectors.astype(np.float64), queries.astype(np.float64)
    similarities = (b / np.linalg.norm(b, axis=1, keepdims=True)) @ (a / np.linalg.norm(a, axis=1, keepdims=True)).T
    oracle = [{parts[i]["id"] for i in np.argsort(-row, kind="stable")[:24]} for row in similarities]
    raw = {mode: [] for mode in modes}
    lexical = []
    print("Quality pass and per-query warmup", flush=True)
    for n, case in enumerate(cases):
        for mode in modes[n % 4:] + modes[:n % 4]:
            raw[mode].append((n, search(mode, n)))
        rows = client.search("milvus_hnsw", data=[case["question"]], anns_field="sparse", filter=f'base_id == "{base}" and version == 1', limit=24, search_params={"metric_type": "BM25", "params": {}}, consistency_level="Session")[0]
        lexical.append([{"id": r["id"], "distance": float(r["distance"])} for r in rows if r["distance"] > 0])
        if (n + 1) % 100 == 0:
            print(f"quality {n + 1}/{len(cases)}", flush=True)
    raw = {mode: [rows for _, rows in sorted(items)] for mode, items in raw.items()}
    save("raw-retrieval.json", {"dense": raw, "shared_bm25": lexical})
    latency = {mode: [] for mode in modes}
    stable = {mode: 0 for mode in modes}
    rng = random.Random(20261108)
    for repetition in range(args.repetitions):
        order = list(range(len(cases)))
        rng.shuffle(order)
        for n in order:
            mode_order = list(modes)
            rng.shuffle(mode_order)
            for mode in mode_order:
                started = time.perf_counter()
                result = search(mode, n)
                latency[mode].append({"query": n, "round": repetition, "ms": (time.perf_counter() - started) * 1000})
                assert len(result) == 24
                stable[mode] += {r["id"] for r in result} == {r["id"] for r in raw[mode][n]}
        print(f"timing round {repetition + 1}/{args.repetitions}", flush=True)
    save("latency.json", latency)
    assert all(stable[mode] == len(cases) * args.repetitions for mode in modes), stable
    save("candidate-stability.json", stable)
    settings = Settings(_env_file=None, app_env="test", embedding_provider="openai_compatible", embedding_model="BAAI/bge-m3", rag_graph_enabled=False)
    embedding = Embeddings(settings)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    summaries, scored = {}, {}
    with Session(engine, expire_on_commit=False) as db:
        db.add(User(id=owner, email="vector-eval@example.test", name="Evaluation", password_hash="unused"))
        db.add(KnowledgeBase(id=base, account_id=owner, name="Isolated vector comparison"))
        db.flush()
        for doc in docs:
            db.add(Document(id=doc["id"], knowledge_base_id=base, account_id=owner, filename=doc["filename"], content_hash=doc["sha256"], object_key="unused", size=doc["characters"], status="ready", index_version=1, embedding_version=embedding.version, collection_name=embedding.collection, chunk_strategy="markdown_sections"))
            attach_parents(db, doc["id"], 1, doc["parts"])
            for p in doc["parts"]:
                db.add(Chunk(id=p["id"], document_id=doc["id"], ordinal=p["ordinal"], page=p["page"], content=p["content"], index_version=1, parent_id=p["parent_id"]))
        db.commit()
        pinned = [*db.scalars(select(Chunk)), *db.scalars(select(ParentChunk)), *db.scalars(select(Document)), *db.scalars(select(User)), *db.scalars(select(KnowledgeBase))]
        scope = document_scope(db, settings, owner, owner, base)
        assert len(scope) == len(docs) and pinned
        for mode in modes:
            for hybrid in (False, True):
                name = mode + ("_shared_bm25" if hybrid else "_dense")
                rows = []
                for n, case in enumerate(cases):
                    rankings = [[r for r in raw[mode][n] if r["distance"] >= 0.35]]
                    if hybrid:
                        rankings.append(lexical[n])
                    hits = rrf(rankings, 24)
                    sources = pack_evidence(materialize(db, settings, owner, owner, base, scope, hits), 5000)
                    rows.append({"query": n, "question": case["question"], **scorer.score_case(case, sources, 5), "sources": sources})
                scored[name] = rows
                summaries[name] = {key: float(np.mean([r[key] for r in rows])) for key in ("recall", "hit", "complete", "reciprocal_rank")}
    for mode in modes:
        values = [r["ms"] for r in latency[mode]]
        summaries[mode] = {"ann_recall_at_24": float(np.mean([len({r['id'] for r in raw[mode][n]} & oracle[n]) / 24 for n in range(len(cases))])), "latency_p50_ms": float(np.percentile(values, 50)), "latency_p95_ms": float(np.percentile(values, 95)), "requests": len(values)}
    differences = {}
    bootstrap = np.random.default_rng(20261108)
    for suffix in ("dense", "shared_bm25"):
        delta = np.array([x["recall"] - y["recall"] for x, y in zip(scored["pg_hnsw_" + suffix], scored["milvus_hnsw_" + suffix], strict=True)])
        means = np.array([bootstrap.choice(delta, len(delta), replace=True).mean() for _ in range(10000)])
        differences[suffix] = {"pg_minus_milvus_recall": float(delta.mean()), "paired_bootstrap_95_ci": np.percentile(means, [2.5, 97.5]).tolist()}
    save("scored-cases.json", scored)
    save("paired.json", differences)
    consistency_rows = []
    sensitivity_queries = random.Random(20261108).sample(range(len(cases)), 50)
    for repetition in range(2):
        for number in sensitivity_queries:
            for level in (("Strong", "Session") if (number + repetition) % 2 else ("Session", "Strong")):
                started = time.perf_counter()
                result = client.search("milvus_hnsw", data=[queries[number].tolist()], anns_field="vector", filter=f'base_id == "{base}" and version == 1', limit=24, search_params={"metric_type": "COSINE", "params": {"ef": 100}}, consistency_level=level)[0]
                consistency_rows.append({"query": number, "round": repetition, "consistency": level, "ms": (time.perf_counter() - started) * 1000, "same_ids_as_main": {r["id"] for r in result} == {r["id"] for r in raw["milvus_hnsw"][number]}})
        print(f"consistency sensitivity round {repetition + 1}/2", flush=True)
    save("consistency.json", consistency_rows)
    for level in ("Strong", "Session"):
        values = [r["ms"] for r in consistency_rows if r["consistency"] == level]
        summaries["consistency_" + level] = {"p50_ms": float(np.percentile(values, 50)), "p95_ms": float(np.percentile(values, 95)), "requests": len(values), "all_same_ids_as_main": all(r["same_ids_as_main"] for r in consistency_rows if r["consistency"] == level)}
    save("manifest.json", manifest)
    save("summary.json", summaries)
    print(json.dumps(summaries, ensure_ascii=False, indent=2), flush=True)
    client.close()
    conn.close()


if __name__ == "__main__":
    main()

"""Reproducible multi-domain BEIR retrieval comparison, isolated from application data."""

import argparse
import csv
import hashlib
import io
import json
import math
import random
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASETS = {
    "scifact": "5f7d1de60b170fc8027bb7898e2efca1",
    "nfcorpus": "a89dba18a62ef92f7d323ec890a0d38d",
    "fiqa": "17918ed23cd04fb15047f73e6c3bd9d9",
    "arguana": "8ad3e3c2a5867cdced806d6503f29b99",
}


def save(out, name, value):
    (out / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def prepare(out):
    documents, cases, manifest = [], [], {}
    for domain, expected_md5 in DATASETS.items():
        target = out / (domain + ".zip")
        url = f"https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/{domain}.zip"
        if not target.exists():
            print("Downloading", domain, flush=True)
            subprocess.run(["curl.exe" if sys.platform == "win32" else "curl", "--noproxy", "*", "--fail", "--location", "--retry", "2", "--silent", "--show-error", "--output", str(target), url], check=True)
        assert hashlib.md5(target.read_bytes(), usedforsecurity=False).hexdigest() == expected_md5
        with zipfile.ZipFile(target) as archive:
            def lines(name):
                return io.TextIOWrapper(archive.open(domain + "/" + name), encoding="utf-8")

            ids = set()
            domain_docs = []
            for line in lines("corpus.jsonl"):
                row = json.loads(line)
                key = domain + ":" + row["_id"]
                text = (row.get("title", "") + "\n" + row["text"]).strip()
                assert key not in ids
                ids.add(key)
                domain_docs.append({"id": key, "domain": domain, "bucket": int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % 100, "text": text})
            questions = {r["_id"]: r["text"] for r in map(json.loads, lines("queries.jsonl"))}
            qrels = {}
            for row in csv.DictReader(lines("qrels/test.tsv"), delimiter="\t"):
                if int(row["score"]) > 0:
                    qrels.setdefault(row["query-id"], {})[domain + ":" + row["corpus-id"]] = int(row["score"])
            missing_targets = [qid for qid in qrels if not set(qrels[qid]) <= ids]
            eligible_queries = sorted(set(qrels) - set(missing_targets))
            selected = sorted(random.Random(20261108).sample(eligible_queries, 100))
            assert all(set(qrels[qid]) <= ids for qid in selected)
            cases.extend({"id": domain + ":" + qid, "domain": domain, "question": questions[qid], "qrels": qrels[qid]} for qid in selected)
            documents.extend(domain_docs)
            manifest[domain] = {"url": url, "official_md5": expected_md5, "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "documents": len(domain_docs), "empty_documents_preserved": sum(not d["text"] for d in domain_docs), "test_queries": len(qrels), "eligible_queries": len(eligible_queries), "excluded_missing_target_queries": missing_targets, "sampled_queries": len(selected)}
            print(domain, manifest[domain], flush=True)
    with (out / "documents.jsonl").open("w", encoding="utf-8") as stream:
        for row in documents:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    save(out, "cases.json", cases)
    save(out, "datasets.json", manifest)


def encode(out, model_path, resume_count=0, batch_size=16):
    import numpy as np
    import torch
    from transformers import AutoModel, AutoTokenizer

    assert torch.cuda.is_available()
    with (out / "documents.jsonl").open(encoding="utf-8") as stream:
        docs = [json.loads(line) for line in stream]
    cases = read(out / "cases.json")
    texts = [d["text"] for d in docs] + [c["question"] for c in cases]
    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
    model = AutoModel.from_pretrained(model_path, local_files_only=True, trust_remote_code=False).to("cuda").eval()
    torch.set_num_threads(4)
    lengths = [len(tokenizer.encode(t, truncation=False)) for t in texts]
    order = sorted(range(len(texts)), key=lambda i: lengths[i])
    result = np.lib.format.open_memmap(out / "all-vectors.npy", mode="r+" if resume_count else "w+", dtype="float32", shape=(len(texts), 1024))
    assert result.shape == (len(texts), 1024)
    if resume_count:
        checkpoint = read(out / "encoding-resume.json")
        assert checkpoint["completed_sorted_rows"] == resume_count
        assert all(hashlib.sha256((out / name).read_bytes()).hexdigest() == digest for name, digest in checkpoint["input_hashes"].items())
        assert np.allclose(np.linalg.norm(result[order[:resume_count]], axis=1), 1, atol=1e-5)
    started = time.monotonic()
    for start in range(resume_count, len(order), batch_size):
        indexes = order[start:start + batch_size]
        batch = tokenizer([texts[i] for i in indexes], padding=True, truncation=True, max_length=1024, return_tensors="pt").to("cuda")
        with torch.inference_mode():
            values = torch.nn.functional.normalize(model(**batch).last_hidden_state[:, 0], p=2, dim=1).cpu().numpy()
        result[indexes] = values
        if (start - resume_count) % 400 == 0:
            result.flush()
            print(f"encoded {start + len(indexes)}/{len(texts)}; {time.monotonic() - started:.1f}s", flush=True)
    result.flush()
    save(out, "encoding.json", {"model": "BAAI/bge-m3", "revision": "5617a9f61b028005a4858fdac845db406aefb181", "precision": "float32", "pooling": "CLS + L2", "max_tokens": 1024, "gpu": torch.cuda.get_device_name(), "torch": torch.__version__, "seconds_this_stage": time.monotonic() - started, "resumed_sorted_rows": resume_count, "batch_size_this_stage": batch_size, "documents": len(docs), "queries": len(cases), "truncated_documents": sum(n > 1024 for n in lengths[:len(docs)]), "truncated_queries": sum(n > 1024 for n in lengths[len(docs):]), "token_lengths": {domain: {"max": max(lengths[i] for i, d in enumerate(docs) if d['domain'] == domain), "truncated": sum(lengths[i] > 1024 for i, d in enumerate(docs) if d['domain'] == domain)} for domain in DATASETS}})


def evaluate(out):
    sys.path.insert(0, str(ROOT / "outputs/vector-db-compare-20261108/deps"))
    import numpy as np
    import psycopg
    from pymilvus import DataType, Function, FunctionType, MilvusClient, connections, utility

    assert not (out / "summary.json").exists(), "Preserve completed experiments"
    with (out / "documents.jsonl").open(encoding="utf-8") as stream:
        docs = [json.loads(line) for line in stream]
    cases = read(out / "cases.json")
    all_vectors = np.load(out / "all-vectors.npy", mmap_mode="r")
    vectors, queries = all_vectors[:len(docs)], all_vectors[len(docs):]
    assert vectors.shape == (len(docs), 1024) and queries.shape == (len(cases), 1024)
    assert np.isfinite(all_vectors).all() and np.allclose(np.linalg.norm(all_vectors, axis=1), 1, atol=1e-5)
    pg = psycopg.connect("host=127.0.0.1 port=45432 dbname=vector_eval user=vector_eval password=isolated-evaluation-only", autocommit=True)
    pg.execute("SET jit=off")
    pg.execute("SET maintenance_work_mem='512MB'")
    pg.execute("SET hnsw.ef_search=100")
    pg.execute("SET hnsw.iterative_scan='strict_order'")
    client = MilvusClient(uri="http://127.0.0.1:49530", timeout=180)
    for name in ("milvus_flat", "milvus_hnsw"):
        if client.has_collection(name):
            client.release_collection(name)
    pg.execute("CREATE TABLE domain_eval (id text PRIMARY KEY, domain text, bucket int, embedding vector(1024))")
    started = time.monotonic()
    with pg.cursor().copy("COPY domain_eval FROM STDIN") as copy:
        for d, v in zip(docs, vectors, strict=True):
            copy.write_row((d["id"], d["domain"], d["bucket"], json.dumps(v.tolist())))
    pg.execute("CREATE INDEX domain_eval_hnsw ON domain_eval USING hnsw (embedding vector_cosine_ops) WITH (m=16,ef_construction=64)")
    pg.execute("CREATE INDEX domain_eval_scope ON domain_eval (domain,bucket)")
    pg.execute("ANALYZE domain_eval")
    setup = {"postgres_seconds": time.monotonic() - started}
    print("PostgreSQL loaded", setup, flush=True)
    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=192)
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=1024)
    schema.add_field("domain", DataType.VARCHAR, max_length=32)
    schema.add_field("bucket", DataType.INT64)
    schema.add_field("text", DataType.VARCHAR, max_length=65535, enable_analyzer=True, analyzer_params={"tokenizer": "standard"})
    schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
    schema.add_function(Function(name="bm25", input_field_names=["text"], output_field_names=["sparse"], function_type=FunctionType.BM25))
    index = client.prepare_index_params()
    index.add_index("vector", index_name="dense", index_type="HNSW", metric_type="COSINE", params={"M": 16, "efConstruction": 64})
    index.add_index("sparse", index_name="bm25", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25", params={"inverted_index_algo": "DAAT_MAXSCORE"})
    index.add_index("domain", index_name="domain_scope", index_type="INVERTED")
    index.add_index("bucket", index_name="bucket_scope", index_type="INVERTED")
    started = time.monotonic()
    client.create_collection("domain_eval", schema=schema, index_params=index, consistency_level="Session")
    for start in range(0, len(docs), 128):
        client.insert("domain_eval", [{**d, "vector": v.tolist()} for d, v in zip(docs[start:start + 128], vectors[start:start + 128], strict=True)])
    client.flush("domain_eval")
    deadline = time.monotonic() + 300
    while int(client.describe_index("domain_eval", "dense").get("indexed_rows", 0)) != len(docs):
        if time.monotonic() > deadline:
            raise RuntimeError("Milvus index build timeout")
        time.sleep(1)
    client.release_collection("domain_eval")
    client.load_collection("domain_eval")
    connections.connect(alias="domain_verify", host="127.0.0.1", port="49530")
    segments = utility.get_query_segment_info("domain_eval", using="domain_verify")
    assert sum(s.num_rows for s in segments) == len(docs) and all(s.state == 3 and s.indexID > 0 for s in segments)
    setup["milvus_seconds_including_bm25"] = time.monotonic() - started
    save(out, "setup.json", {**setup, "segments": [str(s) for s in segments]})
    print("Milvus loaded; computing exact cosine oracle", flush=True)
    a, b = vectors.astype(np.float64), queries.astype(np.float64)
    cosine = (b / np.linalg.norm(b, axis=1, keepdims=True)) @ (a / np.linalg.norm(a, axis=1, keepdims=True)).T
    del a, b
    id_to_index = {d["id"]: i for i, d in enumerate(docs)}
    measurements, details, plans = [], [], {}
    rng = random.Random(20261108)
    for scenario in ("global", "domain", "bucket_1pct"):
        for number, case in enumerate(cases):
            domain = case["domain"]
            bucket = docs[id_to_index[sorted(case["qrels"])[0]]]["bucket"]
            eligible = [i for i, d in enumerate(docs) if d["id"] != case["id"] and (scenario == "global" or (d["domain"] == domain and (scenario != "bucket_1pct" or d["bucket"] == bucket)))]
            eligible_indexes = set(eligible)
            qrels = {key: value for key, value in case["qrels"].items() if id_to_index[key] in eligible_indexes}
            assert qrels
            exact = [docs[i]["id"] for i in sorted(eligible, key=lambda i: (-cosine[number, i], docs[i]["id"]))[:24]]
            sql_filter = " WHERE id <> %(query_id)s" + ("" if scenario == "global" else " AND domain=%(domain)s" + (" AND bucket=%(bucket)s" if scenario == "bucket_1pct" else ""))
            expression = f'id != {json.dumps(case["id"])}' + ("" if scenario == "global" else f' and domain == "{domain}"' + (f" and bucket == {bucket}" if scenario == "bucket_1pct" else ""))
            sql = "SELECT id FROM domain_eval" + sql_filter + " ORDER BY embedding <=> %(vector)s::vector LIMIT 24"

            def search(backend):
                if backend == "pgvector":
                    return [r[0] for r in pg.execute(sql, {"query_id": case["id"], "domain": domain, "bucket": bucket, "vector": json.dumps(queries[number].tolist())}).fetchall()]
                return [r["id"] for r in client.search("domain_eval", data=[queries[number].tolist()], anns_field="vector", filter=expression, limit=24, search_params={"metric_type": "COSINE", "params": {"ef": 100}}, consistency_level="Session")[0]]

            raw = {name: search(name) for name in ("pgvector", "milvus")}
            lexical = [r["id"] for r in client.search("domain_eval", data=[case["question"]], anns_field="sparse", filter=expression, limit=24, search_params={"metric_type": "BM25", "params": {}}, consistency_level="Session")[0] if r["distance"] > 0]
            if domain + ":" + scenario not in plans:
                plans[domain + ":" + scenario] = pg.execute("EXPLAIN (FORMAT JSON) " + sql, {"query_id": case["id"], "domain": domain, "bucket": bucket, "vector": json.dumps(queries[number].tolist())}).fetchone()[0]
            eligible_ids = {docs[i]["id"] for i in eligible}
            for backend, hits in raw.items():
                assert len(hits) == len(set(hits)) and set(hits) <= eligible_ids
                for hybrid in (False, True):
                    ranking = hits
                    if hybrid:
                        totals = {}
                        for branch in (hits, lexical):
                            for rank, key in enumerate(branch, 1):
                                totals[key] = totals.get(key, 0) + 1 / (60 + rank)
                        ranking = sorted(totals, key=lambda key: (-totals[key], key))[:24]
                    top = ranking[:10]
                    gain = sum(qrels.get(key, 0) / math.log2(rank + 1) for rank, key in enumerate(top, 1))
                    ideal = sum(g / math.log2(rank + 1) for rank, g in enumerate(sorted(qrels.values(), reverse=True)[:10], 1))
                    details.append({"query": case["id"], "domain": domain, "scenario": scenario, "backend": backend, "hybrid_shared_bm25": hybrid, "eligible": len(eligible), "returned": len(hits), "expected_candidates": len(exact), "ann_recall24": len(set(hits) & set(exact)) / len(exact), "recall10": len(set(top) & set(qrels)) / len(qrels), "hit10": bool(set(top) & set(qrels)), "ndcg10": gain / ideal, "ids": ranking, "exact_ids": exact, "qrels": qrels})
            for repetition in range(3):
                order = ["pgvector", "milvus"]
                rng.shuffle(order)
                for backend in order:
                    started = time.perf_counter()
                    hits = search(backend)
                    measurements.append({"query": case["id"], "domain": domain, "scenario": scenario, "backend": backend, "round": repetition, "ms": (time.perf_counter() - started) * 1000, "same_ids_as_quality": hits == raw[backend]})
            if (number + 1) % 100 == 0:
                print(f"{scenario}: {number + 1}/{len(cases)}", flush=True)
        save(out, "cases-scored.json", details)
        save(out, "latency.json", measurements)
    summary = []
    for scenario in ("global", "domain", "bucket_1pct"):
        for domain in DATASETS:
            for backend in ("pgvector", "milvus"):
                times = [r["ms"] for r in measurements if (r["scenario"], r["domain"], r["backend"]) == (scenario, domain, backend)]
                for hybrid in (False, True):
                    rows = [r for r in details if (r["scenario"], r["domain"], r["backend"], r["hybrid_shared_bm25"]) == (scenario, domain, backend, hybrid)]
                    summary.append({"scenario": scenario, "domain": domain, "backend": backend, "hybrid_shared_bm25": hybrid, "queries": len(rows), **{key: float(np.mean([r[key] for r in rows])) for key in ("recall10", "hit10", "ndcg10", "ann_recall24", "returned", "eligible")}, "underfilled_queries": sum(r["returned"] < r["expected_candidates"] for r in rows), "p50_ms": float(np.percentile(times, 50)), "p95_ms": float(np.percentile(times, 95))})
    save(out, "pg-plans.json", plans)
    save(out, "summary.json", summary)
    save(out, "input-hashes.json", {name: hashlib.sha256((out / name).read_bytes()).hexdigest() for name in ("documents.jsonl", "cases.json", "datasets.json", "encoding.json")})
    save(out, "run.json", {"scope": "RAG retrieval only; full BEIR corpora, 100 seeded test queries per domain; no generation, reranking or parent expansion", "hybrid": "Identical Milvus standard-tokenizer BM25 candidates shared between vector backends; not a standalone PostgreSQL lexical implementation", "filter": "bucket_1pct is synthetic 1% per-domain access scope, anchored to a positive document; qrels restricted to eligible documents", "latency": "Three warm single-client rounds, randomized backend order; native-client vector serialization included; lexical/oracle/scoring/generation excluded", "versions": {"postgres": pg.execute("SHOW server_version").fetchone()[0], "pgvector": pg.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()[0], "milvus": client.get_server_version()}, "seed": 20261108, "hnsw": {"M": 16, "efConstruction": 64, "efSearch": 100}, "pg_optimizer": "Default planner, B-tree (domain,bucket) and HNSW available; strict_order iterative scans", "milvus_consistency": "Session, same writer client, flushed/indexed/sealed/reloaded frozen corpus", "all_candidate_orders_stable": all(r["same_ids_as_quality"] for r in measurements), "vectors_sha256": hashlib.sha256((out / "all-vectors.npy").read_bytes()).hexdigest(), "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    print(json.dumps(summary, indent=2), flush=True)
    client.close()
    pg.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "encode", "evaluate"))
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/vector-domain-compare-20261108")
    parser.add_argument("--model", default="/models")
    parser.add_argument("--resume-count", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=16)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.stage == "prepare":
        prepare(args.output)
    elif args.stage == "encode":
        encode(args.output, args.model, args.resume_count, args.batch_size)
    else:
        evaluate(args.output)

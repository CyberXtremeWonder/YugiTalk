"""用真实 BGE-M3 和项目父子检索函数评测新旧分块，隔离业务资料。"""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
MODES = ("fixed", "markdown_sections", "auto", "tokens")
SOURCES = (
    "backend/app/rag/chunking.py",
    "backend/app/rag/parser.py",
    "backend/app/rag/hierarchy.py",
    "backend/app/rag/retrieval.py",
    "backend/app/rag/embeddings.py",
    "scripts/evaluate-chunk-profiles.py",
)


def load(path):
    """读取 UTF-8 实验文件。"""
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, value):
    """保存参数、逐题证据和可复核汇总。"""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def digest(path):
    """绑定实际运行的代码与输入，不混用不同快照。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def module(name, path):
    """复用原评测器和已经验证的独立 Milvus schema 工具。"""
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def prepare(args):
    """直接调用生产解析函数，冻结四组切分和去重后的真实编码输入。"""
    sys.path.insert(0, str(ROOT / "backend"))
    from app.rag.parser import parse

    os.environ["BGE_MODEL_PATH"] = str(args.model)
    os.environ["EMBEDDING_MODEL"] = "BAAI/bge-m3"
    args.output.mkdir(parents=True, exist_ok=True)
    if (args.output / "experiment.json").exists():
        raise ValueError("输出目录已有实验，请复用 encode/evaluate 或选择新目录")
    manifest = load(args.dataset / "import.json")
    cases = load(args.dataset / "cases.json")["cases"]
    texts = {
        hashlib.sha256(c["question"].encode()).hexdigest(): c["question"] for c in cases
    }
    counts = {}
    for mode in MODES:
        documents = []
        profiles = Counter()
        for doc in manifest["documents"]:
            content = (args.dataset / "documents" / doc["filename"]).read_bytes()
            assert hashlib.sha256(content).hexdigest() == doc["sha256"]
            parts = parse(content, ".md", mode)
            assert parts and all(0 < len(p["content"]) <= 2000 for p in parts)
            occurrences = Counter()
            for part in parts:
                text = part["content"]
                key = hashlib.sha256(text.encode()).hexdigest()
                texts[key] = text
                # 同一文档中相同内容的相同次序使用稳定身份，控制并列分数排序。
                occurrences[key] += 1
                part["id"] = str(
                    uuid5(NAMESPACE_URL, f"{doc['id']}:{key}:{occurrences[key]}")
                )
                part["vector_key"] = key
            profiles[parts[0].get("profile", mode)] += 1
            documents.append({**doc, "parts": parts})
        save(args.output / (mode + "-documents.json"), documents)
        counts[mode] = {
            "documents": len(documents),
            "children": sum(len(d["parts"]) for d in documents),
            "detected_profiles": dict(profiles),
        }
        print(mode, counts[mode], flush=True)
    save(args.output / "texts.json", texts)
    save(
        args.output / "experiment.json",
        {
            "dataset": str(args.dataset.resolve()),
            "questions": len(cases),
            "passages": manifest["passages"],
            "model": str(args.model.resolve()),
            "tokenizer_sha256": digest(args.model / "tokenizer.json"),
            "cases_sha256": digest(args.dataset / "cases.json"),
            "document_hashes": {d["id"]: d["sha256"] for d in manifest["documents"]},
            "source_hashes": {p: digest(ROOT / p) for p in SOURCES},
            "counts": counts,
            "candidate_limit": 24,
            "dense_threshold": 0.35,
            "rrf_k": 60,
            "top_k": 5,
            "context_characters": 5000,
            "rerank": False,
            "graph": False,
            "max_seq_length": 1024,
            "precision": "float32",
            "batch_size": 8,
            "retrieval_workers": 8,
            "ids": "uuid5(document,text_hash,occurrence)",
            "scope": "MIRACL zh dev judged passage pool; not full corpus or answer evaluation",
        },
    )


def encode(args):
    """相同文本复用既有真实向量，只用 GPU 计算新增输入。"""
    meta = load(args.output / "experiment.json")
    texts = load(args.output / "texts.json")
    cache_meta = {
        "model": str(args.model.resolve()),
        "tokenizer": meta["tokenizer_sha256"],
        "max_seq_length": 1024,
        "pooling": "cls",
        "normalize": True,
        "dtype": "float32",
    }
    assert load(args.cache / "model.json") == cache_meta
    missing = [key for key in texts if not (args.cache / (key + ".npy")).exists()]
    started = time.monotonic()
    import torch
    from transformers import AutoModel, AutoTokenizer

    assert torch.cuda.is_available()
    torch.set_num_threads(4)
    tokenizer = AutoTokenizer.from_pretrained(args.model, local_files_only=True)
    model = (
        AutoModel.from_pretrained(
            args.model, local_files_only=True, trust_remote_code=False
        )
        .to("cuda")
        .eval()
    )
    pool = load(args.model / "1_Pooling/config.json")
    assert pool["pooling_mode_cls_token"] and not pool["pooling_mode_mean_tokens"]
    for start in range(0, len(missing), 8):
        keys = missing[start : start + 8]
        inputs = tokenizer(
            [texts[k] for k in keys],
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=1024,
        ).to("cuda")
        with torch.inference_mode():
            vectors = (
                torch.nn.functional.normalize(
                    model(**inputs).last_hidden_state[:, 0], p=2, dim=1
                )
                .cpu()
                .numpy()
            )
        for key, vector in zip(keys, vectors, strict=True):
            assert vector.shape == (1024,) and np.isfinite(vector).all()
            np.save(args.cache / (key + ".npy"), vector)
        if start % 512 == 0 or start + 8 >= len(missing):
            print(
                f"embedding {min(start + 8, len(missing))}/{len(missing)}", flush=True
            )
    save(
        args.output / "encoding.json",
        {
            "unique_texts": len(texts),
            "new_texts": len(missing),
            "seconds": time.monotonic() - started,
            "torch": torch.__version__,
            "gpu": torch.cuda.get_device_name(),
            "cache": str(args.cache.resolve()),
        },
    )


def evaluate(args):
    """使用真实 SQL 父子关系、授权与版本校验，以及生产检索/打包函数。"""
    sys.path.insert(0, str(ROOT / "backend"))
    from app.core.config import Settings
    from app.core.entities import Base
    from app.identity.models import User
    from app.rag.chunking import strategy_version
    from app.rag.embeddings import Embeddings, VectorIndex
    from app.rag.hierarchy import attach_parents
    from app.rag.models import Chunk, Document, KnowledgeBase, ParentChunk
    from app.rag.retrieval import document_scope, materialize, pack_evidence, rrf
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    helper = module("chunk_benchmark", ROOT / "scripts/benchmark-token-chunking.py")
    scorer = module(
        "retrieval_evaluation", ROOT / "scripts/evaluate-knowledge-retrieval.py"
    )
    meta = load(args.output / "experiment.json")
    assert all(digest(ROOT / p) == sha for p, sha in meta["source_hashes"].items())
    assert digest(args.dataset / "cases.json") == meta["cases_sha256"]
    cases = scorer.load_cases(args.dataset / "cases.json")
    settings = Settings(
        _env_file=None,
        app_env="test",
        embedding_provider="openai_compatible",
        embedding_model="BAAI/bge-m3",
        milvus_uri=args.milvus,
        rag_graph_enabled=False,
    )
    embedding = Embeddings(settings)
    store = VectorIndex(settings)
    owner = str(uuid5(NAMESPACE_URL, "chunk-profile-evaluation-owner"))
    base_id = str(uuid5(NAMESPACE_URL, "chunk-profile-evaluation-base"))
    summary = []
    with httpx.Client(base_url=args.milvus, trust_env=False, timeout=120) as client:
        for mode in MODES:
            result_path = args.output / (mode + "-summary.json")
            if result_path.exists():
                summary.append(load(result_path))
                continue
            documents = load(args.output / (mode + "-documents.json"))
            collection = (
                "profile_eval_"
                + mode
                + "_"
                + digest(args.output / "experiment.json")[:10]
            )
            helper.create_collection(client, collection)
            engine = create_engine("sqlite://")
            Base.metadata.create_all(engine)
            with Session(engine, expire_on_commit=False) as db:
                db.add(
                    User(
                        id=owner,
                        email="evaluation@example.test",
                        name="Evaluation",
                        password_hash="unused",
                    )
                )
                db.add(
                    KnowledgeBase(
                        id=base_id, account_id=owner, name="Isolated MIRACL evaluation"
                    )
                )
                db.flush()
                vectors = []
                for doc in documents:
                    parts = doc["parts"]
                    db.add(
                        Document(
                            id=doc["id"],
                            knowledge_base_id=base_id,
                            account_id=owner,
                            filename=doc["filename"],
                            content_hash=doc["sha256"],
                            object_key="unused",
                            size=doc["characters"],
                            status="ready",
                            index_version=1,
                            embedding_version=embedding.version,
                            collection_name=collection,
                            chunk_strategy=parts[0].get(
                                "strategy_version", strategy_version(mode)
                            ),
                        )
                    )
                    attach_parents(db, doc["id"], 1, parts)
                    for part in parts:
                        db.add(
                            Chunk(
                                id=part["id"],
                                document_id=doc["id"],
                                ordinal=part["ordinal"],
                                page=part["page"],
                                content=part["content"],
                                index_version=1,
                                parent_id=part["parent_id"],
                            )
                        )
                        vector = np.load(args.cache / (part["vector_key"] + ".npy"))
                        assert vector.shape == (1024,) and np.isfinite(vector).all()
                        vectors.append(
                            {
                                "id": part["id"],
                                "text": part["content"],
                                "document_id": doc["id"],
                                "knowledge_base_id": base_id,
                                "index_version": 1,
                                "vector": vector.tolist(),
                            }
                        )
                db.commit()
                pinned = [
                    *db.scalars(select(Chunk)),
                    *db.scalars(select(ParentChunk)),
                    *db.scalars(select(Document)),
                    *db.scalars(select(User)),
                    *db.scalars(select(KnowledgeBase)),
                ]
                for start in range(0, len(vectors), 64):
                    store.upsert(collection, vectors[start : start + 64])
                count = helper.milvus(
                    client,
                    "entities/query",
                    {
                        "collectionName": collection,
                        "filter": "",
                        "outputFields": ["count(*)"],
                        "consistencyLevel": "Strong",
                    },
                )
                assert count[0]["count(*)"] == len(vectors)
                scope = document_scope(db, settings, owner, owner, base_id)
                versions = {key: value[0] for key, value in scope.items()}
                checkpoint = args.output / (mode + "-cases.jsonl")
                rows = (
                    [
                        json.loads(line)
                        for line in checkpoint.read_text(encoding="utf-8").splitlines()
                    ]
                    if checkpoint.exists()
                    else []
                )
                assert [r["number"] for r in rows] == list(range(1, len(rows) + 1))

                def fetch(index, collection=collection, versions=versions):
                    """并发获取真实候选；SQL 物化在主线程按顺序完成。"""
                    question = cases[index]["question"]
                    vector = np.load(
                        args.cache
                        / (hashlib.sha256(question.encode()).hexdigest() + ".npy")
                    ).tolist()

                    async def pair():
                        deadline = time.monotonic() + 60
                        return await asyncio.gather(
                            store.recall(
                                collection,
                                owner,
                                base_id,
                                versions,
                                vector,
                                False,
                                deadline,
                                lambda: None,
                            ),
                            store.recall(
                                collection,
                                owner,
                                base_id,
                                versions,
                                question,
                                True,
                                deadline,
                                lambda: None,
                            ),
                        )

                    return index, asyncio.run(pair())

                started = time.monotonic()
                with ThreadPoolExecutor(max_workers=8) as executor:
                    for index, (dense, sparse) in executor.map(
                        fetch, range(len(rows), len(cases))
                    ):
                        hits = rrf(
                            [
                                [h for h in dense if h["distance"] >= 0.35],
                                [h for h in sparse if h["distance"] > 0],
                            ],
                            24,
                        )
                        candidates = materialize(
                            db, settings, owner, owner, base_id, scope, hits
                        )
                        sources = pack_evidence(candidates, 5000)
                        row = {
                            "number": index + 1,
                            "question": cases[index]["question"],
                            **scorer.score_case(cases[index], sources, 5),
                            "candidate_recall": scorer.score_case(
                                cases[index], candidates, len(candidates) or 1
                            )["recall"],
                            "sources": sources,
                            "raw_dense": dense,
                            "raw_sparse": sparse,
                        }
                        rows.append(row)
                        with checkpoint.open("a", encoding="utf-8") as stream:
                            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                        if len(rows) % 50 == 0 or len(rows) == len(cases):
                            print(f"{mode}: {len(rows)}/{len(cases)}", flush=True)
                assert pinned and len(rows) == len(cases)
                result = {
                    "mode": mode,
                    "questions": len(rows),
                    "errors": 0,
                    "children": len(vectors),
                    "parents": len([p for p in pinned if isinstance(p, ParentChunk)]),
                    "collection": collection,
                    "indexed_count": count[0]["count(*)"],
                    "seconds_with_cached_embeddings": time.monotonic() - started,
                    **{
                        key: float(np.mean([r[key] for r in rows]))
                        for key in (
                            "recall",
                            "hit",
                            "complete",
                            "reciprocal_rank",
                            "candidate_recall",
                        )
                    },
                }
                save(result_path, result)
                summary.append(result)
                save(args.output / "summary.json", summary)
                print(json.dumps(result, ensure_ascii=False), flush=True)
            engine.dispose()


def main():
    """分离准备、GPU 编码和真实生产函数评测，兼容现有两个 Python 环境。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "encode", "evaluate"))
    parser.add_argument("--dataset", type=Path, default=ROOT / "outputs/miracl-zh-dev")
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--cache", type=Path, default=ROOT / "outputs/token-chunking-20261026/vectors"
    )
    parser.add_argument("--milvus", default="http://127.0.0.1:39530")
    args = parser.parse_args()
    {"prepare": prepare, "encode": encode, "evaluate": evaluate}[args.stage](args)


if __name__ == "__main__":
    main()

"""在独立 Milvus 集合中对照字符与 BGE-M3 token 分块，不修改业务资料。"""

import argparse
import hashlib
import json
import math
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise
from pathlib import Path

import httpx
import numpy as np
from tokenizers import Tokenizer


def normalized(text):
    """沿用项目评测的空白及大小写归一化。"""
    return "".join(text.split()).casefold()


def split_tokens(text, tokenizer, size):
    """按原文偏移切块，含特殊 token 不超过上限，10% 重叠取整。"""
    special = tokenizer.num_special_tokens_to_add(False)
    capacity = size - special
    overlap = round(size * 0.1)
    if capacity <= overlap:
        raise ValueError("token 上限过小")
    start = 0
    while start < len(text):
        rest = text[start:]
        encoding = tokenizer.encode(rest, add_special_tokens=False)
        if len(encoding.ids) <= capacity:
            if rest.strip():
                yield start, len(text), rest.strip()
            break
        count = capacity
        end = encoding.offsets[count - 1][1]
        # 重新编码实际原文，处理归一化、共享字符偏移和片段起点的差异。
        while len(tokenizer.encode(rest[:end].strip()).ids) > size:
            count -= 1
            end = encoding.offsets[count - 1][1]
        part = rest[:end].strip()
        if not part or end <= 0:
            raise ValueError("token 边界未前进")
        yield start, start + end, part
        next_start = encoding.offsets[max(1, count - overlap)][0]
        start += max(1, min(next_start, end))


def make_chunks(documents, strategy, tokenizer):
    """保留原始页内章节边界；三子一父使用原文范围消除交叠。"""
    leaves, parents = [], {}
    for doc in documents:
        raw = doc["text"].strip()
        boundaries = [0]
        if strategy != "fixed700":
            boundaries += [
                m.start()
                for m in re.finditer(r"(?m)^#{1,6}[ \t]+[^\n]*$", raw)
                if m.start() > 0
            ]
        boundaries.append(len(raw))
        for section, (begin, end) in enumerate(pairwise(boundaries)):
            text = raw[begin:end].strip()
            if strategy.startswith("token"):
                parts = list(split_tokens(text, tokenizer, int(strategy[5:])))
            else:
                overlap = 70 if strategy == "sections700o10" else 100
                stop = (
                    len(text) if strategy == "fixed700" else max(1, len(text) - overlap)
                )
                parts = [
                    (i, min(i + 700, len(text)), text[i : i + 700].strip())
                    for i in range(0, stop, 700 - overlap)
                    if text[i : i + 700].strip()
                ]
            for index, (start, finish, content) in enumerate(parts):
                parent_id = f"{doc['id']}:{section}:{index // 3}"
                chunk_id = hashlib.md5(
                    f"{doc['id']}:{begin + start}:{begin + finish}:{content}".encode(),
                    usedforsecurity=False,
                ).hexdigest()
                leaf = {
                    "id": chunk_id,
                    "document_id": doc["id"],
                    "content": content,
                    "parent_id": parent_id,
                    "start": begin + start,
                    "end": begin + finish,
                    "tokens": len(tokenizer.encode(content).ids),
                }
                leaves.append(leaf)
                if index % 3 == 0:
                    group = parts[index : index + 3]
                    parents[parent_id] = {
                        "content": text[group[0][0] : group[-1][1]].strip(),
                        "child_count": len(group),
                    }
    return leaves, parents


def milvus(client, endpoint, payload):
    """检查 HTTP 与 Milvus 业务状态，失败直接停止实验。"""
    response = client.post(
        "/v2/vectordb/" + endpoint, json={"dbName": "default", **payload}
    )
    response.raise_for_status()
    data = response.json()
    if data.get("code") != 0:
        raise RuntimeError(data)
    return data.get("data")


def create_collection(client, name):
    """使用与项目相同的 cosine、原生 jieba BM25 和强一致性。"""
    if milvus(client, "collections/has", {"collectionName": name})["has"]:
        return
    milvus(
        client,
        "collections/create",
        {
            "collectionName": name,
            "schema": {
                "autoId": False,
                "enableDynamicField": True,
                "fields": [
                    {
                        "fieldName": "id",
                        "dataType": "VarChar",
                        "isPrimary": True,
                        "elementTypeParams": {"max_length": "36"},
                    },
                    {
                        "fieldName": "vector",
                        "dataType": "FloatVector",
                        "elementTypeParams": {"dim": "1024"},
                    },
                    {
                        "fieldName": "text",
                        "dataType": "VarChar",
                        "elementTypeParams": {
                            "max_length": "8192",
                            "enable_analyzer": "true",
                            "analyzer_params": json.dumps({"tokenizer": "jieba"}),
                        },
                    },
                    {"fieldName": "sparse", "dataType": "SparseFloatVector"},
                ],
                "functions": [
                    {
                        "name": "native_bm25",
                        "type": "BM25",
                        "inputFieldNames": ["text"],
                        "outputFieldNames": ["sparse"],
                    }
                ],
            },
            "indexParams": [
                {
                    "fieldName": "vector",
                    "indexName": "dense_cosine",
                    "metricType": "COSINE",
                    "indexType": "AUTOINDEX",
                },
                {
                    "fieldName": "sparse",
                    "indexName": "native_bm25",
                    "metricType": "BM25",
                    "indexType": "SPARSE_INVERTED_INDEX",
                    "params": {"inverted_index_algo": "DAAT_MAXSCORE"},
                },
            ],
            "params": {"consistencyLevel": "Strong"},
        },
    )


def retrieve(client, name, question, vector, leaves, parents):
    """复现双路 Top24、0.35 阈值、RRF60、父块合并和 Top5/5000 字符。"""
    totals = defaultdict(float)
    for sparse, query in ((False, vector), (True, question)):
        rows = milvus(
            client,
            "entities/search",
            {
                "collectionName": name,
                "data": [query],
                "annsField": "sparse" if sparse else "vector",
                "limit": 24,
                "outputFields": ["id"],
                "consistencyLevel": "Strong",
                "searchParams": {
                    "metricType": "BM25" if sparse else "COSINE",
                    "params": {},
                },
            },
        )
        rows = (
            [row for row in rows if row["distance"] > 0]
            if sparse
            else [row for row in rows if row["distance"] >= 0.35]
        )
        for rank, row in enumerate(rows, 1):
            totals[row["id"]] += 1 / (60 + rank)
    hits = sorted(totals, key=lambda key: (-totals[key], key))[:24]
    parent_counts = Counter(leaves[key]["parent_id"] for key in hits)
    sources, emitted, seen_content = [], set(), set()
    for key in hits:
        leaf = leaves[key]
        parent_id = leaf["parent_id"]
        parent = parents[parent_id]
        merge = parent_counts[parent_id] >= max(
            2, math.ceil(parent["child_count"] * 0.5)
        )
        identity = parent_id if merge else key
        if identity in emitted:
            continue
        emitted.add(identity)
        sources.append(
            {
                "document_id": leaf["document_id"],
                "content": parent["content"] if merge else leaf["content"],
                "id": identity,
                "merged": merge,
            }
        )
    packed, remaining = [], 5000
    for source in sources:
        if source["content"] in seen_content or remaining <= 0:
            continue
        seen_content.add(source["content"])
        packed.append({**source, "content": source["content"][:remaining]})
        remaining -= len(packed[-1]["content"])
        if len(packed) == 5:
            break
    return packed, sources


def score(targets, sources):
    """一条来源完整包含标注引文才计入命中，与原有严格指标一致。"""
    matches = [
        any(
            t["document_id"] == s["document_id"]
            and normalized(t["quote"]) in normalized(s["content"])
            for s in sources
        )
        for t in targets
    ]
    rank = next(
        (
            i
            for i, s in enumerate(sources, 1)
            if any(
                t["document_id"] == s["document_id"]
                and normalized(t["quote"]) in normalized(s["content"])
                for t in targets
            )
        ),
        None,
    )
    return {
        "recall": sum(matches) / len(matches),
        "hit": any(matches),
        "complete": all(matches),
        "mrr": 1 / rank if rank else 0,
    }


def save(path, value):
    """保存可审计的参数、分块和逐题结果。"""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    """全量评测七组，缓存模型输出，支持相同实验快照断点续跑。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--vector-cache", type=Path)
    parser.add_argument("--milvus", default="http://127.0.0.1:39530")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    tokenizer = Tokenizer.from_file(str(args.model / "tokenizer.json"))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    manifest = json.loads((args.dataset / "import.json").read_text(encoding="utf-8"))
    cases = json.loads((args.dataset / "cases.json").read_text(encoding="utf-8"))[
        "cases"
    ]
    documents = [
        {
            **doc,
            "text": (args.dataset / "documents" / doc["filename"]).read_text(
                encoding="utf-8"
            ),
        }
        for doc in manifest["documents"]
    ]
    for doc in documents:
        if hashlib.sha256(doc["text"].encode()).hexdigest() != doc["sha256"]:
            raise ValueError("数据集原文与清单不一致")
    metadata = {
        "dataset": str(args.dataset),
        "questions": len(cases),
        "passages": manifest["passages"],
        "model": str(args.model),
        "tokenizer_sha256": hashlib.sha256(
            (args.model / "tokenizer.json").read_bytes()
        ).hexdigest(),
        "cases_sha256": hashlib.sha256(
            (args.dataset / "cases.json").read_bytes()
        ).hexdigest(),
        "documents": [{k: doc[k] for k in ("id", "sha256")} for doc in documents],
        "token_sizes": [512, 256, 128, 100],
        "special_tokens_included": True,
        "token_overlap": {n: round(n * 0.1) for n in (512, 256, 128, 100)},
        "overlap_ratio": 0.1,
        "character_baselines": {
            "fixed700": 100,
            "sections700": 100,
            "sections700o10": 70,
        },
        "candidate_limit": 24,
        "threshold": 0.35,
        "rrf_k": 60,
        "parent_children": 3,
        "merge_ratio": 0.5,
        "context_characters": 5000,
        "top_k": 5,
        "rerank": False,
        "graph": False,
        "max_seq_length": 1024,
        "precision": "float32",
        "batch_size": 8,
        "retrieval_workers": 8,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    meta_path = args.output / "experiment.json"
    if meta_path.exists() and json.loads(
        meta_path.read_text(encoding="utf-8")
    ) != json.loads(json.dumps(metadata)):
        raise ValueError("实验参数已改变，请使用新的输出目录")
    save(meta_path, metadata)

    import torch
    from transformers import AutoModel, AutoTokenizer

    if not torch.cuda.is_available():
        raise RuntimeError("本实验要求 CUDA，避免意外长时间占用 CPU")
    torch.set_num_threads(4)
    model = (
        AutoModel.from_pretrained(
            args.model, local_files_only=True, trust_remote_code=False
        )
        .to("cuda")
        .eval()
    )
    hf_tokenizer = AutoTokenizer.from_pretrained(args.model, local_files_only=True)
    # 对齐本地 SentenceTransformer 模块：Transformer -> CLS pooling -> Normalize。
    pool = json.loads((args.model / "1_Pooling/config.json").read_text())
    assert pool["pooling_mode_cls_token"] and not pool["pooling_mode_mean_tokens"]
    print(f"GPU: {torch.cuda.get_device_name()}, model loaded", flush=True)
    cache = args.vector_cache or args.output / "vectors"
    cache.mkdir(parents=True, exist_ok=True)
    cache_meta = {
        "model": str(args.model.resolve()),
        "tokenizer": metadata["tokenizer_sha256"],
        "max_seq_length": 1024,
        "pooling": "cls",
        "normalize": True,
        "dtype": "float32",
    }
    cache_meta_path = cache / "model.json"
    if (
        cache_meta_path.exists()
        and json.loads(cache_meta_path.read_text()) != cache_meta
    ):
        raise ValueError("向量缓存对应另一模型配置")
    save(cache_meta_path, cache_meta)
    save(
        args.output / "runtime.json",
        {
            "torch": torch.__version__,
            "gpu": torch.cuda.get_device_name(),
            "vector_cache": str(cache.resolve()),
        },
    )

    def encode(texts, label):
        """仅复用相同文本的真实 BGE-M3 向量，保持与服务相同的 1024 截断。"""
        keys = [hashlib.sha256(text.encode()).hexdigest() for text in texts]
        pending = list(
            dict.fromkeys(key for key in keys if not (cache / (key + ".npy")).exists())
        )
        lookup = dict(zip(keys, texts))
        began = time.monotonic()
        for start in range(0, len(pending), 8):
            batch = pending[start : start + 8]
            inputs = hf_tokenizer(
                [lookup[key] for key in batch],
                padding=True,
                truncation=True,
                max_length=1024,
                return_tensors="pt",
            ).to("cuda")
            with torch.inference_mode():
                vectors = (
                    torch.nn.functional.normalize(
                        model(**inputs).last_hidden_state[:, 0], p=2, dim=1
                    )
                    .cpu()
                    .numpy()
                )
            assert vectors.shape == (len(batch), 1024) and np.isfinite(vectors).all()
            for key, vector in zip(batch, vectors):
                np.save(cache / (key + ".npy"), vector)
            if start % 256 == 0 or start + 8 >= len(pending):
                print(
                    f"{label} embedding {min(start + 8, len(pending))}/{len(pending)}",
                    flush=True,
                )
        return np.stack([np.load(cache / (key + ".npy")) for key in keys]), {
            "new_texts": len(pending),
            "cached_texts": len(texts) - len(pending),
            "seconds": time.monotonic() - began,
        }

    queries, query_timing = encode([case["question"] for case in cases], "queries")
    save(args.output / "query-timing.json", query_timing)
    summaries = []
    with httpx.Client(base_url=args.milvus, timeout=120, trust_env=False) as client:
        for strategy in (
            "fixed700",
            "sections700",
            "sections700o10",
            "token512",
            "token256",
            "token128",
            "token100",
        ):
            final = args.output / (strategy + ".json")
            if final.exists():
                summaries.append(
                    json.loads(final.read_text(encoding="utf-8"))["summary"]
                )
                continue
            leaves, parents = make_chunks(documents, strategy, tokenizer)
            print(f"{strategy}: {len(leaves)} chunks", flush=True)
            save(
                args.output / (strategy + "-chunks.json"),
                {"leaves": leaves, "parents": parents},
            )
            if strategy.startswith("token"):
                assert max(leaf["tokens"] for leaf in leaves) <= int(strategy[5:])
            if max(len(leaf["content"]) for leaf in leaves) > 2000:
                raise ValueError("分块超过业务 embedding 2000 字符限制")
            vectors, timing = encode([leaf["content"] for leaf in leaves], strategy)
            collection = (
                "chunk_eval_"
                + strategy
                + "_"
                + hashlib.sha256(
                    json.dumps(metadata, sort_keys=True).encode()
                ).hexdigest()[:10]
            )
            create_collection(client, collection)
            for start in range(0, len(leaves), 64):
                milvus(
                    client,
                    "entities/upsert",
                    {
                        "collectionName": collection,
                        "data": [
                            {
                                "id": leaf["id"],
                                "text": leaf["content"],
                                "vector": vector.tolist(),
                            }
                            for leaf, vector in zip(
                                leaves[start : start + 64], vectors[start : start + 64]
                            )
                        ],
                    },
                )
            registry = {leaf["id"]: leaf for leaf in leaves}
            rows = []
            checkpoint = args.output / (strategy + "-cases.jsonl")
            if checkpoint.exists():
                rows = [
                    json.loads(line)
                    for line in checkpoint.read_text(encoding="utf-8").splitlines()
                ]

            def evaluate(
                index, collection=collection, registry=registry, parents=parents
            ):
                """独立问题并发查询，共享只读索引，不改变每题的候选和计分。"""
                case = cases[index]
                began = time.monotonic()
                packed, candidates = retrieve(
                    client,
                    collection,
                    case["question"],
                    queries[index].tolist(),
                    registry,
                    parents,
                )
                return {
                    "number": index + 1,
                    "question": case["question"],
                    **score(case["relevant"], packed),
                    "candidate_recall": score(case["relevant"], candidates)["recall"],
                    "seconds": time.monotonic() - began,
                    "sources": packed,
                }

            with ThreadPoolExecutor(max_workers=8) as executor:
                for row in executor.map(evaluate, range(len(rows), len(cases))):
                    rows.append(row)
                    with checkpoint.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                    if len(rows) % 50 == 0 or len(rows) == len(cases):
                        print(
                            f"{strategy} retrieval {len(rows)}/{len(cases)}", flush=True
                        )
            summary = {
                "strategy": strategy,
                "chunks": len(leaves),
                "parents": len(parents),
                "questions": len(rows),
                "mean_tokens": float(np.mean([l["tokens"] for l in leaves])),
                "max_tokens": max(l["tokens"] for l in leaves),
                "truncated_at_1024": sum(l["tokens"] > 1024 for l in leaves),
                "mean_characters": float(np.mean([len(l["content"]) for l in leaves])),
                "embedding": timing,
                "collection": collection,
                **{
                    key: float(np.mean([row[key] for row in rows]))
                    for key in ("recall", "hit", "complete", "mrr", "candidate_recall")
                },
            }
            save(final, {"summary": summary})
            summaries.append(summary)
            save(args.output / "summary.json", summaries)
            print(json.dumps(summary, ensure_ascii=False), flush=True)
    save(args.output / "summary.json", summaries)


if __name__ == "__main__":
    main()

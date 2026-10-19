"""在固定新索引上缓存真实候选，逐组比较边界、重排、预算和候选数。"""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import math
import sys
import time
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings
from app.core.database import create_database
from app.rag.embeddings import Embeddings, VectorIndex, bounded_post
from app.rag.models import Chunk, Document, ParentChunk
from app.rag.rerank import Reranker
from app.rag.retrieval import (
    document_scope,
    materialize,
    pack_evidence,
    rrf,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

spec = importlib.util.spec_from_file_location(
    "retrieval_evaluation", ROOT / "scripts/evaluate-knowledge-retrieval.py"
)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


def read_lines(path: Path, repair: bool = False) -> list[dict]:
    """读取断点文件，只有没有换行的最后一条半成品可以忽略。"""
    if not path.exists():
        return []
    lines = path.read_bytes().splitlines(keepends=True)
    result = []
    for index, line in enumerate(lines):
        if index == len(lines) - 1 and not line.endswith(b"\n"):
            # 先移除半行，避免继续追加时拼接成损坏 JSON。
            if repair:
                path.write_bytes(b"".join(lines[:index]))
            break
        result.append(json.loads(line))
    return result


def append(path: Path, value: dict) -> None:
    """先保存来源再保存排名，支持中断后继续而不重复发出已完成请求。"""
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")


def save(path: Path, value: dict) -> None:
    """保存阶段摘要，避免把未完成实验显示成已完成。"""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


async def candidates(
    settings: Settings,
    base: str,
    scope: dict,
    question: str,
    limits: tuple[int, ...] = (24, 36, 48),
) -> dict:
    """每题只编码一次，按指定候选上限分别请求，保留阈值过滤前的结果。"""
    embeddings = Embeddings(settings)
    groups = {}
    for key, (version, collection) in scope.items():
        groups.setdefault(collection, {})[key] = version
    if set(groups) != {embeddings.collection}:
        raise ValueError("对照库必须使用同一当前混合集合")
    started = time.monotonic()
    deadline = started + 60
    vector = await embeddings.aencode(question, deadline, lambda: None)
    encoding_seconds = time.monotonic() - started

    async def pair(limit: int) -> tuple[str, dict]:
        """不同上限分别请求，避免从近似搜索 Top48 截取冒充真实 Top24。"""
        index = VectorIndex(settings.model_copy(update={"rag_candidate_limit": limit}))
        began = time.monotonic()
        dense, sparse = await asyncio.gather(
            index.recall(
                embeddings.collection,
                "",
                base,
                groups[embeddings.collection],
                vector,
                False,
                deadline,
                lambda: None,
            ),
            index.recall(
                embeddings.collection,
                "",
                base,
                groups[embeddings.collection],
                question,
                True,
                deadline,
                lambda: None,
            ),
        )
        return str(limit), {
            "dense": dense,
            "sparse": sparse,
            "seconds": encoding_seconds + time.monotonic() - began,
        }

    return dict(await asyncio.gather(*(pair(limit) for limit in limits)))


def capture(directory: Path, limit: int | None, validation: bool = False) -> None:
    """使用实际分块、授权和父块合并函数，保存可复核的候选快照。"""
    manifest = json.loads((directory / "import.json").read_text(encoding="utf-8"))
    cases = evaluation.load_cases(directory / "cases.json")
    if limit:
        cases = cases[:limit]
    if manifest["index_status"] != {"ready": len(manifest["documents"])}:
        raise ValueError("必须先完成全部文档索引")
    settings = Settings(
        rag_graph_enabled=False,
        rag_candidate_limit=24,
        rag_context_characters=5000,
        retrieval_min_score=0.35,
        rag_merge_ratio=0.5,
    )
    limits = (24,) if validation else (24, 36, 48)
    engine = create_database(settings)
    target = directory / "ab"
    target.mkdir(exist_ok=True)
    source_path, row_path = target / "sources.jsonl", target / "candidates.jsonl"
    registry = {
        row["id"]: row["source"] for row in read_lines(source_path, repair=True)
    }
    done = read_lines(row_path, repair=True)
    if [row["number"] for row in done] != list(range(1, len(done) + 1)):
        raise ValueError("候选断点次序不一致")
    try:
        with Session(engine, expire_on_commit=False) as db:
            base, owner = manifest["knowledge_base_id"], manifest["owner_id"]
            scope = document_scope(db, settings, owner, owner, base)
            snapshot = {
                "base": base,
                "scope": scope,
                "embedding": Embeddings(settings).version,
                "questions_sha256": hashlib.sha256(
                    (directory / "cases.json").read_bytes()
                ).hexdigest(),
                "limits": list(limits),
                "graph": False,
                "merge_ratio": settings.rag_merge_ratio,
            }
            if validation:
                snapshot["validation"] = {
                    "candidate_limit": 24,
                    "threshold": 0.35,
                    "context_characters": 5000,
                    "selection": "训练集选择后固定；验证集仅比较关闭与开启既有重排",
                }
            meta_path = target / "capture-meta.json"
            if meta_path.exists() and json.loads(
                meta_path.read_text(encoding="utf-8")
            ) != json.loads(json.dumps(snapshot)):
                raise ValueError("索引、数据集或参数发生变化，不能复用旧候选")
            save(meta_path, snapshot)
            # 只读预载并保持对象引用，避免参数回放反复读取同一分块；每题释放事务。
            pinned = [
                *db.scalars(select(Document).where(Document.id.in_(scope))),
                *db.scalars(select(Chunk).where(Chunk.document_id.in_(scope))),
                *db.scalars(
                    select(ParentChunk).where(ParentChunk.document_id.in_(scope))
                ),
            ]
            for number in range(len(done) + 1, len(cases) + 1):
                case = cases[number - 1]
                raw = asyncio.run(
                    candidates(settings, base, scope, case["question"], limits)
                )
                rankings = {}
                configurations = (
                    ((24, 0.35),)
                    if validation
                    else (
                        (24, 0.35),
                        (36, 0.35),
                        (48, 0.35),
                        (24, 0.30),
                        (24, 0.40),
                    )
                )
                for candidate_limit, threshold in configurations:
                    result = raw[str(candidate_limit)]
                    hits = rrf(
                        [
                            [
                                item
                                for item in result["dense"]
                                if item["distance"] >= threshold
                            ],
                            [item for item in result["sparse"] if item["distance"] > 0],
                        ],
                        candidate_limit,
                    )
                    sources = materialize(db, settings, owner, owner, base, scope, hits)
                    keys = []
                    for source in sources:
                        key = source["evidence_id"]
                        keys.append(key)
                        if key not in registry:
                            registry[key] = {
                                name: value
                                for name, value in source.items()
                                if name != "score"
                            }
                            append(source_path, {"id": key, "source": registry[key]})
                        elif (
                            registry[key]["content"],
                            registry[key]["document_id"],
                        ) != (source["content"], source["document_id"]):
                            raise ValueError("相同证据 ID 对应的原文发生变化")
                    rankings[f"c{candidate_limit}-t{threshold:.2f}"] = keys
                db.commit()
                append(
                    row_path,
                    {
                        "number": number,
                        "question": case["question"],
                        "raw": raw,
                        "rankings": rankings,
                    },
                )
                if number % 25 == 0 or number == len(cases):
                    print(f"候选采集 {number}/{len(cases)}", flush=True)
            assert pinned
        with Session(engine) as db:
            if document_scope(db, settings, owner, owner, base) != scope:
                raise ValueError("采集期间文档发生变化，实验不可作为固定快照比较")
        summarize(directory, rerank=False)
    finally:
        engine.dispose()


def text_key(text: str) -> str:
    """模型分数按实际送入的文本缓存，截断长度不同不能共用分数。"""
    return hashlib.sha256(text.encode()).hexdigest()


def rerank_all(directory: Path, timeout: float = 15) -> None:
    """实际调用本地交叉编码模型，再经项目重排协议验证；只复用相同问题文本对。"""
    target = directory / "ab"
    rows = read_lines(target / "candidates.jsonl")
    registry = {
        row["id"]: row["source"] for row in read_lines(target / "sources.jsonl")
    }
    scores_path = target / "reranked.jsonl"
    done = read_lines(scores_path, repair=True)
    secret = dotenv_values(ROOT / ".cache/rag-ab-reranker.env")["RERANK_TEST_KEY"]
    revision = json.loads(
        (ROOT / ".cache/models/bge-reranker-v2-m3/download-revision.json").read_text()
    )
    meta = {
        "model": revision,
        "max_length": 1024,
        "fp16": True,
        "max_characters": 12000,
        "batch_size": 8,
        "capture": json.loads(
            (target / "capture-meta.json").read_text(encoding="utf-8")
        ),
    }
    meta_path = target / "rerank-meta.json"
    if timeout != 15:
        meta["timeout_seconds"] = timeout
    if meta_path.exists() and json.loads(meta_path.read_text(encoding="utf-8")) != meta:
        raise ValueError("重排模型或采集快照变化，不能复用分数")
    save(meta_path, meta)
    if [row["number"] for row in done] != list(range(1, len(done) + 1)):
        raise ValueError("重排断点次序不一致")
    for row in rows[len(done) :]:
        memo = {}
        timings = []

        async def cached_model(
            url, payload, headers, deadline, guard, memo=memo, timings=timings
        ):
            """调用真实服务填充缺失分数，返回项目期望的完整且唯一索引结果。"""
            missing = list(
                dict.fromkeys(
                    text for text in payload["documents"] if text_key(text) not in memo
                )
            )
            if missing:
                started = time.monotonic()
                response = await bounded_post(
                    url,
                    {**payload, "documents": missing, "top_n": len(missing)},
                    headers,
                    deadline,
                    guard,
                )
                results = response.get("results", [])
                if len(results) != len(missing) or {
                    item["index"] for item in results
                } != set(range(len(missing))):
                    raise ValueError("重排服务未返回完整唯一索引")
                for item in results:
                    score = item["relevance_score"]
                    if not isinstance(score, (int, float)) or not math.isfinite(score):
                        raise ValueError("重排分数非法")
                    memo[text_key(missing[item["index"]])] = score
                timings.append(
                    {
                        "documents": len(missing),
                        "http_seconds": time.monotonic() - started,
                        "inference_seconds": response["inference_seconds"],
                    }
                )
            return {
                "results": [
                    {"index": index, "relevance_score": memo[text_key(text)]}
                    for index, text in enumerate(payload["documents"])
                ]
            }

        async def project(row=row, cached_model=cached_model):
            """逐个候选配置使用原有 Reranker，其输入截断与异常校验保持一致。"""
            output = {}
            with patch("app.rag.rerank.bounded_post", cached_model):
                for key, identifiers in row["rankings"].items():
                    size = int(key.split("-")[0][1:])
                    settings = Settings(
                        rag_candidate_limit=size,
                        rag_rerank_url="http://127.0.0.1:18082/rerank",
                        rag_rerank_model="BAAI/bge-reranker-v2-m3",
                        rag_rerank_api_key=secret,
                        rag_rerank_timeout_seconds=timeout,
                        rag_rerank_max_characters=12000,
                    )
                    sources = [registry[item] for item in identifiers]
                    ranked, status = await Reranker(settings).run(
                        row["question"], sources, time.monotonic() + 30, lambda: None
                    )
                    if status != "applied":
                        raise RuntimeError(f"原题{row['number']}重排未执行：{status}")
                    output[key] = [item["evidence_id"] for item in ranked]
            return output

        ranked = asyncio.run(project())
        append(
            scores_path,
            {
                "number": row["number"],
                "rankings": ranked,
                "pair_scores": memo,
                "real_model_calls": timings,
            },
        )
        if row["number"] % 25 == 0 or row["number"] == len(rows):
            print(f"真实重排 {row['number']}/{len(rows)}", flush=True)
            summarize(directory, rerank=True)
    summarize(directory, rerank=True)


def summarize(directory: Path, rerank: bool) -> None:
    """质量回放不重复计作真实延迟；保留每题分数用于配对比较。"""
    target = directory / "ab"
    cases = evaluation.load_cases(directory / "cases.json")
    metadata = json.loads((target / "capture-meta.json").read_text(encoding="utf-8"))
    budgets = (5000,) if "validation" in metadata else (5000, 6500, 8000)
    captured = read_lines(target / "candidates.jsonl")
    registry = {
        row["id"]: row["source"] for row in read_lines(target / "sources.jsonl")
    }
    ranked = (
        {row["number"]: row for row in read_lines(target / "reranked.jsonl")}
        if rerank
        else {}
    )
    configs = {}
    for row in captured:
        case = cases[row["number"] - 1]
        modes = {"rrf": row["rankings"]}
        if row["number"] in ranked:
            modes["rerank"] = ranked[row["number"]]["rankings"]
        for mode, rankings in modes.items():
            for key, identifiers in rankings.items():
                for budget in budgets:
                    sources = pack_evidence(
                        [registry[item] for item in identifiers], budget
                    )
                    score = evaluation.score_case(case, sources, 5)
                    configs.setdefault(f"{key}-{mode}-ctx{budget}", []).append(
                        {
                            "number": row["number"],
                            "seconds": 0,
                            **score,
                        }
                    )
    summaries = {}
    for key, rows in configs.items():
        summary = evaluation.aggregate(rows, 5)
        del summary["latency_p50_seconds"], summary["latency_p95_seconds"]
        summaries[key] = summary
    save(
        target / "quality.json",
        {
            "expected_questions": len(cases),
            "summaries": summaries,
            "latency_note": "质量缓存回放，无端到端延迟结论；各组必须相同题数后再比较",
            "cases": configs,
        },
    )
    for key in (
        "c24-t0.35-rrf-ctx5000",
        "c24-t0.35-rerank-ctx5000",
        "c36-t0.35-rerank-ctx5000",
        "c48-t0.35-rerank-ctx5000",
    ):
        if key in summaries:
            print(key, json.dumps(summaries[key], ensure_ascii=False), flush=True)


def main() -> None:
    """显式选择阶段；每一步均保留断点，不更改业务默认参数。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["capture", "rerank", "summarize"])
    parser.add_argument(
        "--directory", type=Path, default=ROOT / "outputs/miracl-zh-sections"
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--rerank-timeout", type=float, default=15, help="重排请求超时秒数，范围(0,15]"
    )
    parser.add_argument(
        "--validation",
        action="store_true",
        help="采集固定24候选方案，汇总仅用5000字符，不调参",
    )
    args = parser.parse_args()
    if not 0 < args.rerank_timeout <= 15:
        parser.error("--rerank-timeout 必须大于0且不超过15秒")
    if args.stage == "capture":
        capture(args.directory, args.limit, args.validation)
    elif args.stage == "rerank":
        rerank_all(args.directory, args.rerank_timeout)
    else:
        summarize(args.directory, rerank=True)


if __name__ == "__main__":
    main()

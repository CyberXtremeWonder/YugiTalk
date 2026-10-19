"""只读重放固定抽样问题，定位 MIRACL 候选、融合和证据打包损失。"""

import asyncio
import json
import random
import sys
import time
from collections import Counter
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings
from app.core.database import create_database
from app.rag.embeddings import Embeddings, VectorIndex
from app.rag.hierarchy import join_overlaps
from app.rag.models import Chunk
from app.rag.parser import parse
from app.rag.retrieval import (
    document_scope,
    materialize,
    pack_evidence,
    rrf,
)
from sqlalchemy import select
from sqlalchemy.orm import Session


def normalize(value: str) -> str:
    """沿用基线的完整引文匹配口径。"""
    return "".join(value.split()).casefold()


def covered(target: dict, sources: list[dict]) -> bool:
    """只在目标文档内检查完整原文，不把同包其他段落算成命中。"""
    return any(
        source["document_id"] == target["document_id"]
        and normalize(target["quote"]) in normalize(source["content"])
        for source in sources
    )


async def recall(settings: Settings, scope: dict, question: str) -> tuple[list, list]:
    """一次查询嵌入并行读取两路原始候选，不修改服务参数或索引。"""
    embeddings, store = Embeddings(settings), VectorIndex(settings)
    groups = {}
    for key, (version, collection) in scope.items():
        groups.setdefault(collection, {})[key] = version
    if set(groups) != {embeddings.collection}:
        raise ValueError("诊断仅适用于本轮全部使用同一当前混合集合的知识库")
    deadline = time.monotonic() + 60
    vector = await embeddings.aencode(question, deadline, lambda: None)
    return await asyncio.gather(
        store.recall(
            embeddings.collection,
            "",
            BASE,
            groups[embeddings.collection],
            vector,
            False,
            deadline,
            lambda: None,
        ),
        store.recall(
            embeddings.collection,
            "",
            BASE,
            groups[embeddings.collection],
            question,
            True,
            deadline,
            lambda: None,
        ),
    )


def main() -> None:
    """固定分层抽样并逐层核对，保存包含逐题计数的审计报告。"""
    directory = ROOT / "outputs/miracl-zh-train"
    manifest = json.loads((directory / "import.json").read_text(encoding="utf-8"))
    original = json.loads(
        max((directory / "evaluation").glob("retrieval-*.json")).read_text(
            encoding="utf-8"
        )
    )
    cases = json.loads((directory / "cases.json").read_text(encoding="utf-8"))["cases"]
    settings = Settings(rag_graph_enabled=False)
    engine = create_database(settings)
    owner = manifest["owner_id"]
    if manifest["knowledge_base_id"] != BASE:
        raise ValueError("知识库不是本轮 MIRACL 测试库")
    rng = random.Random(20261016)
    groups = {
        "zero": [row for row in original["cases"] if not row["hit"]],
        "partial": [
            row for row in original["cases"] if row["hit"] and not row["complete"]
        ],
        "complete": [row for row in original["cases"] if row["complete"]],
    }
    sample = {
        row["number"]
        for key, count in (("zero", 20), ("partial", 20), ("complete", 10))
        for row in rng.sample(groups[key], count)
    }
    sample.update((8, 11, 13))
    files = {}
    leaf_ranges = {}
    theoretical = Counter()
    try:
        with Session(engine) as db:
            scope = document_scope(db, settings, owner, owner, BASE)
            chunks = {
                item.id: {"document_id": item.document_id, "content": item.content}
                for item in db.scalars(
                    select(Chunk).where(Chunk.document_id.in_(scope))
                )
            }
        for document in manifest["documents"]:
            raw = (
                (directory / "documents" / document["filename"])
                .read_text(encoding="utf-8")
                .strip()
            )
            parts = parse(raw.encode(), ".md")
            files[document["id"]] = raw
            entries = []
            for part in parts:
                start = (part["ordinal"] - 1) * 600
                segment = raw[start : start + 700]
                start += len(segment) - len(segment.lstrip())
                key = str(
                    uuid5(
                        NAMESPACE_URL,
                        f"{document['id']}:{scope[document['id']][0]}:{part['ordinal']}",
                    )
                )
                if key not in chunks or chunks[key]["content"] != part["content"]:
                    raise ValueError("本地原文与已发布分块不一致，禁止推断位置")
                entries.append((key, start, start + len(part["content"])))
                theoretical["leaf_count"] += 1
                theoretical["leaf_multi_marker"] += (
                    part["content"].count("MIRACL_DOCID:") >= 2
                )
            leaf_ranges[document["id"]] = entries
            parent_texts = [
                join_overlaps([part["content"] for part in parts[i : i + 3]])
                for i in range(0, len(parts), 3)
            ]
            targets = [
                target
                for case in cases
                for target in case["relevant"]
                if target["document_id"] == document["id"]
            ]
            for target in targets:
                theoretical["positive_labels"] += 1
                theoretical["fit_leaf"] += any(
                    normalize(target["quote"]) in normalize(part["content"])
                    for part in parts
                )
                theoretical["fit_parent"] += any(
                    normalize(target["quote"]) in normalize(text)
                    for text in parent_texts
                )
        results = []
        for number in sorted(sample):
            case = cases[number - 1]
            dense, sparse = asyncio.run(recall(settings, scope, case["question"]))
            filtered_dense = [
                row for row in dense if row["distance"] >= settings.retrieval_min_score
            ]
            sparse = [row for row in sparse if row["distance"] > 0]
            fused = rrf([filtered_dense, sparse], settings.rag_candidate_limit)
            with Session(engine) as db:
                sources = materialize(db, settings, owner, owner, BASE, scope, fused)
            packed = pack_evidence(sources, 5000)
            larger = pack_evidence(sources, 8000)
            stages = {
                "dense_raw": dense,
                "dense_filtered": filtered_dense,
                "bm25": sparse,
                "union": [*filtered_dense, *sparse],
                "fused24": fused,
            }
            diagnostics = []
            for target in case["relevant"]:
                raw = files[target["document_id"]]
                start = raw.find(target["quote"])
                if start < 0:
                    raise ValueError("标准引文不在导入原文中")
                end = start + len(target["quote"])
                eligible = {
                    key
                    for key, begin, finish in leaf_ranges[target["document_id"]]
                    if begin < end and finish > start
                }
                overlap = {
                    stage: any(hit["id"] in eligible for hit in hits)
                    for stage, hits in stages.items()
                }
                strict = {
                    "fused_leaf24": covered(
                        target, [chunks[row["id"]] for row in fused]
                    ),
                    "materialized_all": covered(target, sources),
                    "top5_untruncated": covered(target, sources[:5]),
                    "packed5000": covered(target, packed),
                    "packed8000": covered(target, larger),
                }
                diagnostics.append(
                    {
                        "document_id": target["document_id"],
                        "quote": target["quote"],
                        "candidate_overlap": overlap,
                        "strict_coverage": strict,
                        "dense_rank": next(
                            (
                                i
                                for i, hit in enumerate(dense, 1)
                                if hit["id"] in eligible
                            ),
                            None,
                        ),
                        "bm25_rank": next(
                            (
                                i
                                for i, hit in enumerate(sparse, 1)
                                if hit["id"] in eligible
                            ),
                            None,
                        ),
                    }
                )
            row = {
                "number": number,
                "question": case["question"],
                "targets": diagnostics,
                "materialized_count": len(sources),
                "final_count": len(packed),
                "merged_count": sum(bool(item.get("merged")) for item in sources),
                "truncated_count": sum(item["truncated"] for item in packed),
                "final_previews": [
                    {
                        "filename": item["filename"],
                        "merged": item.get("merged", False),
                        "characters": len(item["content"]),
                        "preview": item["content"][:180],
                    }
                    for item in packed
                ],
            }
            results.append(row)
            print(f"诊断 {len(results)}/{len(sample)}：原题 {number}", flush=True)
        total = Counter()
        for row in results:
            for target in row["targets"]:
                total["labels"] += 1
                total.update(
                    {
                        "overlap_" + key: value
                        for key, value in target["candidate_overlap"].items()
                    }
                )
                total.update(
                    {
                        "strict_" + key: value
                        for key, value in target["strict_coverage"].items()
                    }
                )
        report = {
            "selection": "固定种子从零召回、部分命中、完整命中各抽20、20、10题，另加原题8、11、13；不是无偏总体样本",
            "candidate_overlap_definition": "原始段落字符范围与候选叶块有交集，只用于定位，不代表原文完整或答案充分",
            "sample_questions": len(results),
            "theoretical": dict(theoretical),
            "totals": dict(total),
            "queries_with_truncation": sum(
                row["truncated_count"] > 0 for row in results
            ),
            "mean_final_sources": sum(row["final_count"] for row in results)
            / len(results),
            "mean_merged_candidates": sum(row["merged_count"] for row in results)
            / len(results),
            "cases": results,
        }
        (directory / "diagnosis.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {key: value for key, value in report.items() if key != "cases"},
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        engine.dispose()


BASE = "e6c758e2-0f72-4a08-868d-02f32017c17b"

if __name__ == "__main__":
    main()

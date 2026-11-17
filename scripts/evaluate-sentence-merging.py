"""仅合并结构化文档的短句，保持父块与检索配置不变进行配对实验。"""

import argparse
import copy
import hashlib
import importlib.util
import re
import sys
from collections import Counter
from itertools import pairwise
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "profile_evaluation", ROOT / "scripts/evaluate-chunk-profiles.py"
)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)
MODES = ("sentence", "merged", "markdown_sections")


def merged_spans(parent, tokenizer, target=200, maximum=256):
    """顺序合并完整句子到目标长度；超长句沿用已有窗口和 10% 重叠。"""
    from app.rag.chunking import windows

    positions = sorted(
        {
            0,
            len(parent),
            *(
                match.end()
                for match in re.finditer(
                    r"[。！？!?](?:[”’\"']|\s)*|(?m:\n(?=\s*(?:[-*+] |\d+[.、)])))",
                    parent,
                )
            ),
        }
    )
    group_start, group_end = None, None
    for begin, end in pairwise(positions):
        sentence = parent[begin:end]
        if not sentence.strip():
            continue
        if (
            len(tokenizer.encode(sentence.strip()).ids) > maximum
            or len(sentence.strip()) > 2000
        ):
            if group_start is not None:
                yield group_start, group_end
                group_start, group_end = None, None
            for start, finish in windows(
                sentence,
                maximum,
                "tokens",
                round(maximum * 0.1),
                tokenizer,
                max_characters=2000,
            ):
                yield begin + start, begin + finish
            continue
        if group_start is not None and (
            len(tokenizer.encode(parent[group_start:end].strip()).ids) > maximum
            or len(parent[group_start:end].strip()) > 2000
        ):
            yield group_start, group_end
            group_start = None
        if group_start is None:
            group_start = begin
        group_end = end
        if len(tokenizer.encode(parent[group_start:group_end].strip()).ids) >= target:
            yield group_start, group_end
            group_start, group_end = None, None
    if group_start is not None:
        yield group_start, group_end


def merge_document(document, tokenizer):
    """逐个复用原父块，保持原文、父块身份和来源页不变。"""
    result = {key: value for key, value in document.items() if key != "parts"}
    result["parts"] = []
    for part in document["parts"]:
        if "parent_content" not in part:
            continue
        parent = part["parent_content"]
        for index, (begin, end) in enumerate(merged_spans(parent, tokenizer)):
            child = {
                "page": part["page"],
                "ordinal": len(result["parts"]) + 1,
                "content": parent[begin:end].strip(),
                "parent_group": part["parent_group"],
                "profile": part["profile"],
                "strategy_version": part["strategy_version"],
                **({"parent_content": parent} if index == 0 else {}),
            }
            assert (
                child["content"] and len(tokenizer.encode(child["content"]).ids) <= 256
            )
            result["parts"].append(child)
    before = [
        (p["parent_group"], p["page"], p["parent_content"])
        for p in document["parts"]
        if "parent_content" in p
    ]
    after = [
        (p["parent_group"], p["page"], p["parent_content"])
        for p in result["parts"]
        if "parent_content" in p
    ]
    assert before == after
    occurrences = Counter()
    for part in result["parts"]:
        key = hashlib.sha256(part["content"].encode()).hexdigest()
        occurrences[key] += 1
        part["id"] = str(
            uuid5(NAMESPACE_URL, f"{document['id']}:{key}:{occurrences[key]}")
        )
        part["vector_key"] = key
    return result


def prepare(args):
    """冻结原句子方案、合并方案和旧章节参考，复用相同父块。"""
    sys.path.insert(0, str(ROOT / "backend"))
    from tokenizers import Tokenizer

    args.output.mkdir(parents=True, exist_ok=True)
    if (args.output / "experiment.json").exists():
        raise ValueError("已有实验，请选择新的输出目录或继续 encode/evaluate")
    previous = evaluation.load(args.previous / "experiment.json")
    assert all(
        evaluation.digest(ROOT / p) == sha
        for p, sha in previous["source_hashes"].items()
    )
    assert evaluation.digest(args.dataset / "cases.json") == previous["cases_sha256"]
    tokenizer = Tokenizer.from_file(str(args.model / "tokenizer.json"))
    assert (
        evaluation.digest(args.model / "tokenizer.json") == previous["tokenizer_sha256"]
    )
    tokenizer.no_truncation()
    tokenizer.no_padding()
    original = evaluation.load(args.previous / "auto-documents.json")
    variants = {
        "sentence": original,
        "merged": [merge_document(doc, tokenizer) for doc in original],
        "markdown_sections": evaluation.load(
            args.previous / "markdown_sections-documents.json"
        ),
    }
    cases = evaluation.load(args.dataset / "cases.json")["cases"]
    texts = {
        hashlib.sha256(c["question"].encode()).hexdigest(): c["question"] for c in cases
    }
    counts = {}
    for mode, documents in variants.items():
        evaluation.save(args.output / (mode + "-documents.json"), documents)
        leaves = [part for doc in documents for part in doc["parts"]]
        for part in leaves:
            texts[part["vector_key"]] = part["content"]
        counts[mode] = {"documents": len(documents), "children": len(leaves)}
        print(mode, counts[mode], flush=True)
    meta = copy.deepcopy(previous)
    meta["counts"] = counts
    meta["source_hashes"]["scripts/evaluate-sentence-merging.py"] = evaluation.digest(
        Path(__file__)
    )
    meta["previous_experiment_sha256"] = evaluation.digest(
        args.previous / "experiment.json"
    )
    meta["sentence_merging"] = {
        "target_tokens": 200,
        "max_tokens": 256,
        "include_special_tokens": True,
        "cross_parent": False,
        "overlap_between_complete_sentence_groups": 0,
        "long_sentence_overlap_tokens": 26,
        "parent_boundary": "exactly retained from previous auto/structured parse",
        "short_parent_or_tail": "may remain below 200 tokens; never padded",
    }
    evaluation.save(args.output / "texts.json", texts)
    evaluation.save(args.output / "experiment.json", meta)


def main():
    """沿用已经验证的真实 GPU 编码及生产检索函数评测入口。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "encode", "evaluate"))
    parser.add_argument("--dataset", type=Path, default=ROOT / "outputs/miracl-zh-dev")
    parser.add_argument(
        "--previous", type=Path, default=ROOT / "outputs/chunk-profiles-20261027"
    )
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--cache", type=Path, default=ROOT / "outputs/token-chunking-20261026/vectors"
    )
    parser.add_argument("--milvus", default="http://127.0.0.1:39530")
    args = parser.parse_args()
    evaluation.MODES = MODES
    {"prepare": prepare, "encode": evaluation.encode, "evaluate": evaluation.evaluate}[
        args.stage
    ](args)


if __name__ == "__main__":
    main()

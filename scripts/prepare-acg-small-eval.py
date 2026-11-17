"""Prepare a fixed balanced ACG-SimpleQA subset and an answer-blind local corpus."""

import hashlib
import json
import random
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/acg_simpleqa_small_20261029"
SEED = 20261029


def norm(title):
    return unicodedata.normalize("NFKC", title).replace("_", " ").strip()


def grams(title):
    return {title[i : i + 2] for i in range(len(title) - 1)}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cases = [
        json.loads(line)
        for line in (ROOT / "outputs/acg_simpleqa/acg_simpleqa.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    grouped = defaultdict(list)
    for index, case in enumerate(cases):
        grouped[case["category"]].append({**case, "original_index": index})
    rng = random.Random(SEED)
    selected = []
    for category in sorted(grouped):
        selected.extend(rng.sample(grouped[category], 40))
    rng.shuffle(selected)
    assert len(selected) == 200
    targets = {
        norm(unquote(urlsplit(url).path).lstrip("/"))
        for case in selected
        for url in case["urls"]
    }
    corpus = ROOT / "outputs/Moegirl_acg_zh/MoeGirlPedia_zh_cleaned_latest.jsonl"
    titles, matched = [], {}
    source_hash = hashlib.sha256()
    with corpus.open("rb") as stream:
        for index, raw in enumerate(stream):
            source_hash.update(raw)
            record = json.loads(raw)
            title = norm(record["title"])
            titles.append(title)
            if title in targets:
                matched[title] = index
    # Candidates depend only on page titles, never on questions' reference answers.
    inverted = defaultdict(list)
    for index, title in enumerate(titles):
        if title not in targets:
            for gram in grams(title):
                inverted[gram].append(index)
    distractors = set()
    for target in sorted(targets):
        counts = Counter(
            index for gram in grams(target) for index in inverted.get(gram, [])
        )
        ranked = sorted(
            counts,
            key=lambda i: (
                -counts[i] / max(1, len(grams(target) | grams(titles[i]))),
                i,
            ),
        )
        distractors.update(ranked[:2])
    wanted = set(matched.values()) | distractors
    corpus_rows = []
    with corpus.open("rb") as stream:
        for index, raw in enumerate(stream):
            if index in wanted:
                corpus_rows.append((index, raw))
    data = b"".join(
        raw if raw.endswith(b"\n") else raw + b"\n" for _, raw in corpus_rows
    )
    (OUT / "corpus.jsonl").write_bytes(data)
    (OUT / "acg_simpleqa.jsonl").write_text(
        "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in selected),
        encoding="utf-8",
    )
    previous = json.loads(
        (ROOT / "outputs/acg_simpleqa/dataset_manifest.json").read_text()
    )
    manifest = {
        **previous,
        "rows": 200,
        "seed": SEED,
        "sampling": "40 per category; shuffled",
        "original_rows": len(cases),
        "categories": dict(Counter(c["category"] for c in selected)),
    }
    (OUT / "dataset_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    coverage = {
        "source_sha256": source_hash.hexdigest(),
        "target_pages": len(targets),
        "matched_pages": len(matched),
        "missing_pages": sorted(targets - set(matched)),
        "distractor_records": len(distractors),
        "records": len(corpus_rows),
        "bytes": len(data),
        "corpus_sha256": hashlib.sha256(data).hexdigest(),
        "target_titles": sorted(matched),
        "distractor_titles": sorted(titles[i] for i in distractors),
        "cases_with_all_reference_pages": sum(
            all(
                norm(unquote(urlsplit(u).path).lstrip("/")) in matched
                for u in c["urls"]
            )
            for c in selected
        ),
    }
    (OUT / "corpus_manifest.json").write_text(
        json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    reference = OUT / "reference"
    reference.mkdir(exist_ok=True)
    (reference / "simpleqa_eval.py").write_bytes(
        (ROOT / "outputs/acg_simpleqa/reference/simpleqa_eval.py").read_bytes()
    )
    print(
        json.dumps(
            {
                k: v
                for k, v in coverage.items()
                if k not in ["target_titles", "distractor_titles"]
            },
            ensure_ascii=True,
        )
    )


if __name__ == "__main__":
    main()

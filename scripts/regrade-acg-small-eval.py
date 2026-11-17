"""Uniformly regrade saved predictions; never regenerate or alter the answers."""

import importlib.util
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.core.config import Settings

OUT = ROOT / "outputs/acg_simpleqa_small_20261029"


def main():
    spec = importlib.util.spec_from_file_location(
        "acg_eval", ROOT / "scripts/evaluate-acg-simpleqa.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    template = module.grader_template(OUT / "reference/simpleqa_eval.py")
    settings = Settings()
    model = settings.rag_grader_model or settings.model_id
    key = (
        settings.rag_grader_api_key.get_secret_value()
        or settings.model_api_key.get_secret_value()
    )
    rows = {}
    for line in (OUT / "results.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        rows[row["index"]] = row
    assert len(rows) == 200
    stopped = Event()

    def grade(row):
        if stopped.is_set():
            return None
        row = dict(row)
        row["initial_grade"] = row["grade"]
        if row.get("run_status") == "waiting_input":
            row["grade"] = "NOT_ATTEMPTED"
            return row
        if "prediction" not in row:
            return row
        body = {
            "model": model,
            "temperature": 0,
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": template.format(
                        question=row["question"],
                        target=row["answer"],
                        predicted_answer=row["prediction"],
                    ),
                }
            ],
        }
        if model == "deepseek-flash":
            body["thinking"] = {"type": "disabled"}
        with httpx.Client(timeout=120, trust_env=False) as client:
            for attempt in range(3):
                if stopped.is_set():
                    return None
                try:
                    response = (
                        client.post(
                            settings.model_base_url.rstrip("/") + "/chat/completions",
                            headers={"Authorization": "Bearer " + key},
                            json=body,
                        )
                        .raise_for_status()
                        .json()
                    )
                    letter = response["choices"][0]["message"]["content"].strip()
                    row["grade"] = {
                        "A": "CORRECT",
                        "B": "INCORRECT",
                        "C": "NOT_ATTEMPTED",
                    }[letter]
                    row["grader_response"] = letter
                    row["grader_usage"] = response.get("usage")
                    row["grading_method"] = "official_template_thinking_disabled"
                    row.pop("error", None)
                    return row
                except (httpx.HTTPError, KeyError, ValueError) as error:
                    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 402:
                        stopped.set()
                        return None
                    if attempt == 2:
                        row["grade"] = "ERROR"
                        row["error"] = "grader_failed_after_3_attempts"
                        return row
                    time.sleep(attempt + 1)

    saved = {}
    progress = OUT / "regraded_results.jsonl"
    if progress.exists():
        for line in progress.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            saved[row["index"]] = row
    with ThreadPoolExecutor(max_workers=2) as pool:
        for row in pool.map(grade, [rows[i] for i in sorted(rows) if i not in saved]):
            if row is None:
                continue
            saved[row["index"]] = row
            with progress.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(json.dumps({"graded": len(saved), "grade": row["grade"]}), flush=True)
    if len(saved) != 200:
        raise SystemExit("Uniform grading interrupted; partial grades saved, original results preserved")
    original = OUT / "results_before_uniform_grading.jsonl"
    if not original.exists():
        original.write_bytes((OUT / "results.jsonl").read_bytes())
    (OUT / "results.jsonl").write_text(
        "".join(json.dumps(saved[i], ensure_ascii=False) + "\n" for i in sorted(saved)),
        encoding="utf-8",
    )
    summary = json.loads((OUT / "summary.json").read_text(encoding="utf-8"))
    counts = Counter(r["grade"] for r in saved.values())
    summary.update(
        {
            "grades": dict(counts),
            "accuracy": counts["CORRECT"] / 200,
            "grader_thinking": "disabled",
            "grading_note": "Uniformly graded all saved predictions; no answer regenerated.",
        }
    )
    for category in summary["categories"]:
        subset = [r for r in saved.values() if r["category"] == category]
        counts = Counter(r["grade"] for r in subset)
        summary["categories"][category] = {
            "total": len(subset),
            **counts,
            "accuracy": counts["CORRECT"] / len(subset),
        }
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()

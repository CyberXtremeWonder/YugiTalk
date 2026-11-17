"""用正常 RAG API 和官方 SimpleQA 三分类模板评测完整 ACG-SimpleQA。"""

import argparse
import ast
import hashlib
import json
import os
import queue
import statistics
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings
from rag_eval_adapter import RagNeedsClarification, rag_session


def grader_template(path: Path) -> str:
    """只解析官方模板字面量，不执行下载的 Python 代码。"""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "GRADER_TEMPLATE"
            for target in node.targets
        ):
            value = node.value
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
                value = value.func.value
            return str(ast.literal_eval(value)).strip()
    raise ValueError("Official SimpleQA grader template is missing")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, default=ROOT / "outputs/acg_simpleqa")
    parser.add_argument("--workers", type=int, default=2, choices=[1, 2])
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    directory = args.directory.resolve()
    state = json.loads((directory / "import.json").read_text(encoding="utf-8"))
    config = dotenv_values(ROOT / "backend/.env.rag-test")
    for key, value in config.items():
        if value is not None and key.startswith("RAG_TEST_"):
            os.environ[key] = value
    os.environ["RAG_TEST_KNOWLEDGE_BASE_ID"] = state["knowledge_base_id"]
    os.environ["RAG_TEST_AGENT_VERSION_ID"] = state["agent_version_id"]
    settings = Settings()
    if settings.model_provider != "openai_compatible":
        raise ValueError("This evaluation requires the configured OpenAI-compatible grader")
    template = grader_template(directory / "reference/simpleqa_eval.py")
    cases = [json.loads(line) for line in (directory / "acg_simpleqa.jsonl").read_text(
        encoding="utf-8-sig",
    ).splitlines() if line.strip()]
    if args.limit:
        cases = cases[:args.limit]
    path = directory / "results.jsonl"
    results = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                results[row["index"]] = row
    jobs: queue.Queue[tuple[int, dict]] = queue.Queue()
    for index, case in enumerate(cases):
        if results.get(index, {}).get("grade") not in {"CORRECT", "INCORRECT", "NOT_ATTEMPTED"}:
            jobs.put((index, case))
    lock = threading.Lock()
    stopped = threading.Event()
    judge_model = settings.rag_grader_model or settings.model_id
    judge_key = settings.rag_grader_api_key.get_secret_value() or (
        settings.model_api_key.get_secret_value()
    )

    def worker() -> None:
        """每个工作线程复用一次登录与连接，避免批量登录触发限流。"""
        with ExitStack() as sessions, httpx.Client(timeout=120, trust_env=False) as judge:
            rag = sessions.enter_context(rag_session())
            rotate_at = time.monotonic() + min(3600, settings.session_ttl_seconds / 2)
            while not stopped.is_set():
                try:
                    index, case = jobs.get_nowait()
                except queue.Empty:
                    return
                started = time.monotonic()
                row = {"index": index, **case}
                try:
                    if time.monotonic() >= rotate_at:
                        sessions.close()
                        rag = sessions.enter_context(rag_session())
                        rotate_at = time.monotonic() + min(3600, settings.session_ttl_seconds / 2)
                    previous = results.get(index, {})
                    output = (
                        {"answer": previous["prediction"], "sources": previous.get("sources", []),
                         "run_id": previous.get("run_id"),
                         "conversation_id": previous.get("conversation_id"),
                         "model": previous.get("answer_model")}
                        if "prediction" in previous and previous.get("grade") == "ERROR"
                        else rag(case["question"])
                    )
                    row["prediction"] = output["answer"]
                    row["sources"] = output.get("sources", [])
                    row["run_id"] = output.get("run_id")
                    row["conversation_id"] = output.get("conversation_id")
                    row["answer_model"] = output.get("model")
                    row["rag_seconds"] = (
                        previous["rag_seconds"]
                        if "prediction" in previous and previous.get("grade") == "ERROR"
                        else round(time.monotonic() - started, 3)
                    )
                    prompt = template.format(
                        question=case["question"], target=case["answer"],
                        predicted_answer=output["answer"],
                    )
                    judge_body = {
                        "model": judge_model, "temperature": 0, "max_tokens": 1024,
                        "messages": [{"role": "user", "content": prompt}],
                    }
                    if judge_model == "deepseek-flash":
                        judge_body["thinking"] = {"type": "disabled"}
                    response = judge.post(
                        settings.model_base_url.rstrip("/") + "/chat/completions",
                        headers={"Authorization": "Bearer " + judge_key},
                        json=judge_body,
                    ).raise_for_status().json()
                    letter = response["choices"][0]["message"]["content"].strip()
                    row["grade"] = {"A": "CORRECT", "B": "INCORRECT", "C": "NOT_ATTEMPTED"}[
                        letter
                    ]
                    row["grader_response"] = letter
                    row["grader_usage"] = response.get("usage")
                    row["prediction"] = output["answer"]
                    row["answer"] = case["answer"]
                except RagNeedsClarification as error:
                    row.update(error.result)
                    row["prediction"] = ""
                    row["sources"] = []
                    row["grade"] = "NOT_ATTEMPTED"
                    row["grading_method"] = "waiting_for_clarification_without_answer"
                except (httpx.HTTPError, RuntimeError, TimeoutError, KeyError, ValueError) as error:
                    row["grade"] = "ERROR"
                    row["error"] = type(error).__name__ + ": " + str(error)[:500]
                    if (isinstance(error, httpx.HTTPStatusError)
                        and error.response.status_code == 402
                    ) or "model_request_rejected" in str(error):
                        stopped.set()
                row["seconds"] = round(time.monotonic() - started, 3)
                with lock:
                    results[index] = row
                    with path.open("a", encoding="utf-8") as target:
                        target.write(json.dumps(row, ensure_ascii=False) + "\n")
                        target.flush()
                    print(json.dumps({"completed": len(results), "total": len(cases),
                                      "index": index, "grade": row["grade"],
                                      "seconds": row["seconds"]}), flush=True)
                jobs.task_done()

    workers = min(args.workers, settings.user_concurrency, settings.workspace_concurrency)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker) for _ in range(workers)]
        for future in futures:
            future.result()
    selected = [results[i] for i in range(len(cases)) if i in results]
    grades = Counter(row["grade"] for row in selected)
    categories = {}
    for category in sorted({row["category"] for row in selected}):
        subset = [row for row in selected if row["category"] == category]
        counts = Counter(row["grade"] for row in subset)
        categories[category] = {"total": len(subset), **counts,
                                "accuracy": counts["CORRECT"] / len(subset)}
    summary = {
        "dataset": json.loads((directory / "dataset_manifest.json").read_text(encoding="utf-8")),
        "knowledge_base_id": state["knowledge_base_id"], "document_id": state["document_id"],
        "document_ids": state.get("document_ids", [state["document_id"]]),
        "agent_version_id": state["agent_version_id"], "answer_model": settings.model_id,
        "grader_model": judge_model, "grader_template_sha256": hashlib.sha256(
            template.encode(),
        ).hexdigest(), "total": len(selected), "grades": dict(grades),
        "expected_total": len(cases), "stopped_early": stopped.is_set(),
        "accuracy": grades["CORRECT"] / len(cases) if len(selected) == len(cases) else None,
        "categories": categories,
        "median_rag_seconds": statistics.median(
            row.get("rag_seconds", row["seconds"]) for row in selected
        ),
    }
    (directory / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()

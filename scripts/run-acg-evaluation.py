"""等待完整百科索引就绪后自动执行全量 ACG-SimpleQA，保存进度与报告。"""

import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "outputs/acg_simpleqa"


def main() -> None:
    state = json.loads((DIRECTORY / "import.json").read_text(encoding="utf-8"))
    config = dotenv_values(ROOT / "backend/.env.rag-test")
    status = {"stage": "waiting_for_index", "started_at": datetime.now(UTC).isoformat()}

    def save() -> None:
        """写入独立进度文件，供长任务期间查看。"""
        (DIRECTORY / "pipeline.json").write_text(
            json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    with httpx.Client(base_url=config["RAG_TEST_API_URL"], timeout=30, trust_env=False) as client:
        def login() -> None:
            """会话过期后重新登录，保留长时间评测的正常鉴权。"""
            client.post(
                "/api/v1/auth/login", headers={"Origin": config["RAG_TEST_ORIGIN"]},
                json={"email": config["RAG_TEST_ACCOUNT"], "password": config["RAG_TEST_PASSWORD"]},
            ).raise_for_status()

        login()
        save()
        while True:
            response = client.get(
                f"/api/v1/knowledge-bases/{state['knowledge_base_id']}/documents",
            )
            if response.status_code == 401:
                login()
                continue
            response.raise_for_status()
            document = next(row for row in response.json()["items"]
                            if row["id"] == state["document_id"])
            status["document"] = document
            status["updated_at"] = datetime.now(UTC).isoformat()
            save()
            if document["status"] == "ready":
                corpus = json.loads((DIRECTORY / "corpus_manifest.json").read_text(encoding="utf-8"))
                progress = document["progress"]
                if (
                    progress.get("processed_records") != corpus["records"]
                    or progress.get("processed_bytes") != corpus["bytes"]
                    or document["content_hash"] != corpus["sha256"]
                ):
                    status["stage"] = "import_validation_failed"
                    save()
                    raise RuntimeError("Imported corpus manifest does not match the full source")
                break
            if document["status"] == "failed":
                status["stage"] = "import_failed"
                save()
                raise RuntimeError(str(document.get("error_code")))
            time.sleep(30)
    status["stage"] = "evaluating"
    save()
    with (DIRECTORY / "evaluation.log").open("a", encoding="utf-8") as log:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/evaluate-acg-simpleqa.py"), "--workers", "2"],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=False,
        )
    if result.returncode:
        status["stage"] = "evaluation_failed"
        status["returncode"] = result.returncode
        save()
        raise RuntimeError("Evaluation failed; see evaluation.log")
    summary = json.loads((DIRECTORY / "summary.json").read_text(encoding="utf-8"))
    lines = [
        "# ACG 知识库完整评测", "",
        f"- 知识库：`{state['knowledge_base_id']}`",
        f"- 原文：`{state['source']}`",
        f"- 已处理记录：{document['progress'].get('processed_records')}",
        f"- 已索引分块：{document['progress'].get('indexed_chunks')}",
        "- 本次未构建全量知识图谱。",
        f"- 测试集修订：`{summary['dataset']['revision']}`，共 {summary['total']} 题。",
        f"- 回答模型：`{summary['answer_model']}`；评分模型：`{summary['grader_model']}`。",
        "- 使用官方 SimpleQA 三分类模板；评分模型与公开榜单可能不同，结果不可直接等同榜单分数。",
        f"- 正确率（含错误请求作为未正确计入分母）：{summary['accuracy']:.2%}。",
        f"- 结果分布：`{summary['grades']}`。",
        f"- RAG 回答耗时中位数：{summary['median_rag_seconds']:.2f} 秒。", "",
        "| 分类 | 题数 | 正确率 |", "| --- | ---: | ---: |",
    ]
    for category, values in summary["categories"].items():
        lines.append(f"| {category} | {values['total']} | {values['accuracy']:.2%} |")
    lines += ["", "逐题问题、参考答案、预测、引用、评分和耗时见 `results.jsonl`。", "",
              ("[测试集](https://huggingface.co/datasets/Papersnake/ACG-SimpleQA)；"
               "[官方评分模板](https://github.com/openai/simple-evals/blob/main/simpleqa_eval.py)。")]
    (DIRECTORY / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    status["stage"] = "complete"
    status["completed_at"] = datetime.now(UTC).isoformat()
    status["summary"] = summary
    save()
    print(json.dumps(status, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()

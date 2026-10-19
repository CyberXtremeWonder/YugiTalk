"""通过真实对话接口检查 Fast、Grader、改写和回答，保留逐题结果。"""

import argparse
import importlib.util
import json
import random
import time
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "evaluation", ROOT / "scripts/evaluate-knowledge-retrieval.py"
)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


def source_texts(client: httpx.Client, sources: list[dict]) -> list[dict]:
    """聊天引用只有标识，须验证权限和证据版本后取回原文才能评分。"""
    result = []
    for source in sources:
        if source.get("truncated"):
            # 引用元数据未记录截断长度，不能拿完整原文冒充实际提供的证据。
            raise ValueError("truncated_source_length_unavailable")
        params = {
            "index_version": source["index_version"],
            "evidence_id": source["evidence_id"],
        }
        if source.get("merged") and source.get("parent_id"):
            params["parent_id"] = source["parent_id"]
        response = client.get(f"/api/v1/sources/{source['chunk_id']}", params=params)
        response.raise_for_status()
        result.append({**source, "content": response.json()["content"]})
    return result


def score_answer(client: httpx.Client, case: dict, row: dict) -> None:
    """仅为正常完成的任务评分，截断长度未知时显式留空而非伪造分数。"""
    if row["run"]["status"] != "succeeded":
        return
    try:
        row["evidence_sources"] = source_texts(client, row["sources"])
    except ValueError as error:
        row.pop("evidence_score", None)
        row["evidence_score_error"] = str(error)
        return
    row.pop("evidence_score_error", None)
    row["evidence_score"] = evaluation.score_case(case, row["evidence_sources"], 5)


def main():
    """单并发执行有限测试；不上传LangSmith、不修改助手或RAG参数。"""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=ROOT / "outputs/rag-deepseek-smoke"
    )
    parser.add_argument("--sample-size", type=int, default=8)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    dataset = ROOT / "outputs/miracl-zh-dev"
    manifest = json.loads((dataset / "import.json").read_text(encoding="utf-8"))
    plan = json.loads((dataset / "validation-plan.json").read_text(encoding="utf-8"))
    excluded = set(plan["exact_question_overlap_ids"])
    eligible = [
        r
        for r in evaluation.load_cases(dataset / "cases.json")
        if r["query_id"] not in excluded
    ]
    if not 4 <= args.sample_size <= len(eligible):
        parser.error(f"--sample-size 必须在4至{len(eligible)}之间")
    selected = random.Random(42).sample(eligible, args.sample_size)
    cases = [{**r, "kind": "dev"} for r in selected]
    for start in (0, 2):
        pair = selected[start : start + 2]
        labels = {
            json.dumps(t, sort_keys=True, ensure_ascii=False): t
            for r in pair
            for t in r["relevant"]
        }
        cases.append(
            {
                "kind": "complex",
                "question": "请分别回答以下两个问题："
                + "；".join(r["question"] for r in pair),
                "relevant": list(labels.values()),
            }
        )
    cases.extend(
        [
            {
                "kind": "no_knowledge",
                "question": "虚构协议 ZQX-TEST-94731 的首个发布版本号和作者分别是谁？仅根据知识库回答。",
                "expected_no_results": True,
            },
            {"kind": "ambiguous", "question": "它的创始人是谁？"},
        ]
    )
    values = dotenv_values(ROOT / "backend/.env.rag-test")
    path = args.output / "results.json"
    rows = (
        json.loads(path.read_text(encoding="utf-8"))["cases"] if path.exists() else []
    )
    old_plan = args.output / "cases.json"
    if old_plan.exists() and json.loads(old_plan.read_text(encoding="utf-8")) != cases:
        raise ValueError("题集已变化，请使用新的输出目录")
    old_plan.write_text(
        json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with httpx.Client(
        base_url=values["RAG_TEST_API_URL"], timeout=30, trust_env=False
    ) as client:
        try:
            login = client.post(
                "/api/v1/auth/login",
                json={
                    "email": values["RAG_TEST_ACCOUNT"],
                    "password": values["RAG_TEST_PASSWORD"],
                },
                headers={"Origin": values["RAG_TEST_ORIGIN"]},
            )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise SystemExit(
                "无法连接项目后端，尚未调用模型。请先启动 API、Worker 和检索依赖，"
                "并核对 backend/.env.rag-test 中的 RAG_TEST_API_URL。"
                "当前 Docker 开发环境的 API 默认地址为 http://127.0.0.1:18000。"
            ) from None
        login.raise_for_status()
        headers = {
            "Origin": values["RAG_TEST_ORIGIN"],
            "X-CSRF-Token": login.json()["csrf_token"],
        }
        agents = client.get("/api/v1/me/agents", params={"limit": 100})
        agents.raise_for_status()
        versions = {r["version_id"]: r for r in agents.json()["items"]}
        version = values["RAG_TEST_AGENT_VERSION_ID"]
        if version not in versions:
            raise ValueError("评测助手版本不在当前助手列表中，须检查配置")
        for case, row in zip(cases, rows, strict=False):
            score_answer(client, case, row)
        if rows:
            saved = json.loads(path.read_text(encoding="utf-8"))
            saved["cases"] = rows
            path.write_text(
                json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        for number, case in enumerate(cases[len(rows) :], len(rows) + 1):
            started = time.monotonic()
            response = client.post(
                "/api/v1/me/conversations",
                json={"title": f"DeepSeek RAG 验证 {number}"},
                headers=headers,
            )
            response.raise_for_status()
            conversation = response.json()["id"]
            response = client.post(
                "/api/v1/runs",
                json={
                    "conversation_id": conversation,
                    "agent_version_id": version,
                    "message": case["question"],
                    "knowledge_base_id": manifest["knowledge_base_id"],
                },
                headers={**headers, "Idempotency-Key": str(uuid4())},
            )
            response.raise_for_status()
            run = response.json()
            deadline = started + 240
            while (
                run["status"] in {"queued", "running"} and time.monotonic() < deadline
            ):
                time.sleep(0.7)
                response = client.get(f"/api/v1/runs/{run['id']}")
                response.raise_for_status()
                run = response.json()
            if run["status"] in {
                "queued",
                "running",
                "waiting_input",
                "waiting_approval",
            }:
                client.post(
                    f"/api/v1/runs/{run['id']}/cancel", headers=headers
                ).raise_for_status()
            response = client.get(
                f"/api/v1/conversations/{conversation}/messages", params={"limit": 100}
            )
            response.raise_for_status()
            answers = [r for r in response.json()["items"] if r["role"] == "assistant"]
            answer = answers[-1] if answers else {"content": "", "sources": []}
            row = {
                "number": number,
                "kind": case["kind"],
                "question": case["question"],
                "run": run,
                "answer": answer["content"],
                "sources": answer["sources"],
                "seconds": round(time.monotonic() - started, 3),
            }
            score_answer(client, case, row)
            rows.append(row)
            path.write_text(
                json.dumps(
                    {
                        "expected": len(cases),
                        "knowledge_base_id": manifest["knowledge_base_id"],
                        "agent_config": versions[version]["config"],
                        "cases": rows,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(
                number,
                case["kind"],
                run["status"],
                run.get("error_code"),
                row["seconds"],
                flush=True,
            )
            if (
                run["status"] == "failed"
                and run.get("error_code") != "rag_structured_output_invalid"
            ):
                raise RuntimeError(
                    "真实流程失败，已保存错误码与任务ID；先定位再继续，避免重复计费"
                )


if __name__ == "__main__":
    main()

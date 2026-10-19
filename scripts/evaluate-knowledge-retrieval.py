"""使用现有登录和知识库搜索接口评测召回，不创建对话或调用生成模型。"""

import argparse
import csv
import getpass
import json
import math
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def normalize(value: str) -> str:
    """忽略空白与英文大小写，仍要求原文逐字覆盖，不冒充语义判断。"""
    return re.sub(r"\s+", "", value).casefold()


def load_cases(path: Path) -> list[dict[str, Any]]:
    """读取标注 JSON 或现有问答 CSV，拒绝把未标注样例当成无答案样例。"""
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as source:
            cases = list(csv.DictReader(source))
    else:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        cases = data["cases"] if isinstance(data, dict) else data
    if not isinstance(cases, list) or not cases or len(cases) > 10000:
        raise ValueError("问题集须包含 1–10000 个问题")
    for number, item in enumerate(cases, 1):
        if not isinstance(item, dict) or not isinstance(item.get("question"), str):
            raise TypeError(f"第 {number} 题须为对象，question 须为字符串")
        if not 1 <= len(item["question"].strip()) <= 2000:
            raise ValueError(f"第 {number} 题问题长度须在 1–2000 字符之间")
        relevant = item.get("relevant")
        if relevant is not None:
            if not isinstance(relevant, list) or not relevant:
                raise ValueError(
                    f"第 {number} 题 relevant 必须非空；未标注请删除该字段"
                )
            identities = set()
            for target in relevant:
                if (
                    not isinstance(target, dict)
                    or not target
                    or set(target) - {"document_id", "filename", "chunk_id", "quote"}
                ):
                    raise ValueError(f"第 {number} 题的证据格式不正确")
                if not any(
                    target.get(key) for key in ("document_id", "filename", "chunk_id")
                ):
                    raise ValueError(
                        f"第 {number} 题证据须标注文档 ID、文件名或分块 ID"
                    )
                if any(
                    not isinstance(value, str) or not value.strip()
                    for value in target.values()
                ):
                    raise ValueError(f"第 {number} 题证据字段须为非空字符串")
                identity = json.dumps(target, sort_keys=True, ensure_ascii=False)
                if identity in identities:
                    raise ValueError(f"第 {number} 题存在重复证据")
                identities.add(identity)
        if item.get("expected_no_results") not in (None, True, False):
            raise ValueError(f"第 {number} 题 expected_no_results 须为布尔值")
        if relevant and item.get("expected_no_results"):
            raise ValueError(f"第 {number} 题不能同时标注相关证据和无答案")
        answer = item.get("answer")
        if answer is not None and not isinstance(answer, str):
            raise ValueError(f"第 {number} 题 answer 须为字符串")
    return cases


def evidence_matches(target: dict[str, str], sources: list[dict[str, Any]]) -> bool:
    """按文档或分块身份定位，带引文时要求一条返回原文覆盖引文。"""
    candidates = []
    for source in sources:
        if any(
            target.get(key) and source.get(key) != target[key]
            for key in ("document_id", "filename")
        ):
            continue
        if target.get("chunk_id") and target["chunk_id"] not in {
            source.get("chunk_id"),
            *source.get("child_ids", []),
        }:
            continue
        candidates.append(source)
    if not target.get("quote"):
        return bool(candidates)
    # 不跨文档拼接；被父块合并或上下文截断后的实际可见文本就是评测对象。
    return any(
        normalize(target["quote"]) in normalize(str(source.get("content", "")))
        for source in candidates
    )


def score_case(
    case: dict[str, Any], sources: list[dict[str, Any]], k: int
) -> dict[str, Any]:
    """正例计算标注证据召回，负例统计空结果，未标注题仅提供答案原文覆盖代理值。"""
    top = sources[:k]
    result: dict[str, Any] = {"returned": len(top), "returned_total": len(sources)}
    targets = case.get("relevant")
    if targets:
        matched = [evidence_matches(target, top) for target in targets]
        rank = next(
            (
                index
                for index, source in enumerate(top, 1)
                if any(evidence_matches(target, [source]) for target in targets)
            ),
            None,
        )
        result.update(
            evidence_matched=matched,
            recall=sum(matched) / len(targets),
            hit=any(matched),
            complete=all(matched),
            reciprocal_rank=1 / rank if rank else 0,
            precision=sum(
                any(evidence_matches(target, [source]) for target in targets)
                for source in top
            )
            / k,
        )
    elif case.get("expected_no_results"):
        result["negative_empty"] = not top
    else:
        result["unlabeled"] = True
    if isinstance(case.get("answer"), str) and case["answer"].strip():
        result["answer_verbatim_covered"] = any(
            normalize(case["answer"]) in normalize(str(source.get("content", "")))
            for source in top
        )
    return result


def aggregate(rows: list[dict[str, Any]], k: int) -> dict[str, Any]:
    """失败题不冒充低召回或高召回，单独报告完成率与分母。"""
    valid = [row for row in rows if "error" not in row]
    positive = [row for row in valid if "recall" in row]
    negative = [row for row in valid if "negative_empty" in row]
    answers = [row for row in valid if "answer_verbatim_covered" in row]
    durations = sorted(row["seconds"] for row in valid)

    def average(items: list[dict[str, Any]], key: str) -> float | None:
        """没有有效标注时返回空值，避免把缺失评测伪装成零分。"""
        return sum(item[key] for item in items) / len(items) if items else None

    return {
        "k": k,
        "total": len(rows),
        "completed": len(valid),
        "errors": len(rows) - len(valid),
        "completion_rate": len(valid) / len(rows),
        "labeled_positive": len(positive),
        "labeled_negative": len(negative),
        "unlabeled": sum(row.get("unlabeled", False) for row in valid),
        "recall_at_k": average(positive, "recall"),
        "hit_at_k": average(positive, "hit"),
        "complete_evidence_at_k": average(positive, "complete"),
        "precision_at_k": average(positive, "precision"),
        "mrr_at_k": average(positive, "reciprocal_rank"),
        "negative_empty_rate": average(negative, "negative_empty"),
        "answer_verbatim_coverage_proxy": average(answers, "answer_verbatim_covered"),
        "latency_p50_seconds": durations[math.ceil(len(durations) * 0.5) - 1]
        if durations
        else None,
        "latency_p95_seconds": durations[math.ceil(len(durations) * 0.95) - 1]
        if durations
        else None,
    }


def run_cases(
    client: httpx.Client,
    headers: dict[str, str],
    base: str,
    cases: list[dict[str, Any]],
    k: int,
) -> list[dict[str, Any]]:
    """单并发调用已有检索接口，不新增资料、对话、任务或评测平台上传。"""
    rows = []
    stop_code: str | None = None
    for number, case in enumerate(cases, 1):
        row: dict[str, Any] = {"number": number, "question": case["question"]}
        for key in ("relevant", "expected_no_results", "answer"):
            if key in case:
                row[key] = case[key]
        started = time.monotonic()
        try:
            if stop_code:
                row["error"] = "not_executed_after_" + stop_code
            else:
                response = client.post(
                    f"/api/v1/knowledge-bases/{base}/search",
                    headers=headers,
                    json={"query": case["question"]},
                )
                response.raise_for_status()
                sources = response.json()["items"]
                if not isinstance(sources, list) or any(
                    not isinstance(source, dict)
                    or not isinstance(source.get("content"), str)
                    for source in sources
                ):
                    raise ValueError("invalid_search_response")
                row.update(score_case(case, sources, k))
                row["sources"] = [
                    {
                        key: source.get(key)
                        for key in (
                            "document_id",
                            "filename",
                            "chunk_id",
                            "parent_id",
                            "index_version",
                            "score",
                        )
                    }
                    | {"preview": source["content"][:300]}
                    for source in sources[:k]
                ]
        except httpx.HTTPStatusError as error:
            row["error"] = f"http_{error.response.status_code}"
            if error.response.status_code in {401, 403, 404, 429}:
                stop_code = row["error"]
        except httpx.HTTPError as error:
            row["error"] = type(error).__name__
            stop_code = row["error"]
        except (ValueError, KeyError, TypeError):
            row["error"] = "invalid_search_response"
        row["seconds"] = round(time.monotonic() - started, 3)
        rows.append(row)
        print(
            f"[{number}/{len(cases)}] {row.get('error', '完成')} · {row['seconds']:.3f}s",
            flush=True,
        )
    return rows


def format_report(report: dict[str, Any]) -> str:
    """生成中文摘要，注明指标分母、实际检索层级和未标注限制。"""
    summary = report["summary"]
    labels = {
        "completion_rate": "请求完成率",
        "recall_at_k": "证据召回率 Recall",
        "hit_at_k": "命中率 Hit",
        "complete_evidence_at_k": "完整证据命中率",
        "precision_at_k": "Precision",
        "mrr_at_k": "MRR",
        "negative_empty_rate": "负例空结果率",
        "answer_verbatim_coverage_proxy": "参考答案逐字覆盖率（代理值）",
    }
    lines = [
        "# 当前知识库检索评测",
        "",
        f"知识库 ID：{report['knowledge_base_id']}",
        f"时间：{report['created_at']}",
        "",
        f"共 {summary['total']} 题，完成 {summary['completed']} 题，错误 {summary['errors']} 题。",
        (
            f"有效标注正例 {summary['labeled_positive']} 题，负例 {summary['labeled_negative']} 题，"
            f"未标注 {summary['unlabeled']} 题。"
        ),
        "",
        "| 指标 | 结果 |",
        "| --- | --- |",
    ]
    for key, label in labels.items():
        value = summary[key]
        lines.append(
            f"| {label} @{summary['k']} | {value:.2%} |"
            if value is not None
            else f"| {label} @{summary['k']} | 无有效标注／不适用 |"
        )
    for key, label in (("latency_p50_seconds", "P50"), ("latency_p95_seconds", "P95")):
        value = summary[key]
        lines.append(
            f"| {label} 耗时 | {value:.3f}s |"
            if value is not None
            else f"| {label} 耗时 | 无数据 |"
        )
    lines += [
        "",
        (
            "评测对象是搜索接口最终返回的原文证据，包含融合、父块合并和上下文截断，"
            "不等于向量初始候选召回，也不包含聊天中的模型重排、评分、重写与答案生成。"
        ),
        (
            "Recall、Hit、完整证据命中率、Precision 和 MRR 只统计成功的标注正例；"
            "请同时查看请求完成率，避免忽略失败题。Precision 的分母固定为 K。"
        ),
        (
            "未标注题不能计算真实召回率。参考答案逐字覆盖率只检测整段答案是否出现在一条返回原文中，"
            "不评价语义正确性，也不能代替召回率。"
        ),
        "负例空结果率只检查检索结果是否为空，不评价聊天拒答是否正确。",
        "命中标注只能说明找到了已标注证据；漏标其他正确证据会影响指标。",
        "",
        "逐题问题、原文预览、来源身份、错误和耗时见同目录 JSON 报告。",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    """读取私有评测配置并执行检索；缺少密码时隐藏输入，不打印凭证。"""
    parser = argparse.ArgumentParser(description="当前知识库召回评测，不调用生成模型")
    parser.add_argument("--env-file", type=Path, default=ROOT / "backend/.env.rag-test")
    parser.add_argument(
        "--cases",
        type=Path,
        default=ROOT / "outputs/agent_memory_qa/agent_memory_50_qa.csv",
    )
    parser.add_argument("--base", help="覆盖配置中的知识库 ID")
    parser.add_argument("--k", type=int, choices=range(1, 6), default=5)
    parser.add_argument("--limit", type=int, help="只评测前 N 题，最多 10000 题")
    parser.add_argument("--timeout", type=float, default=65)
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/rag-evaluation")
    parser.add_argument(
        "--list-bases", action="store_true", help="登录后列出可访问的知识库 ID"
    )
    parser.add_argument(
        "--export-template", type=Path, help="将问题集导出为待标注 JSON，不连接服务"
    )
    args = parser.parse_args()
    if args.timeout <= 0 or (args.limit is not None and not 1 <= args.limit <= 10000):
        raise ValueError("timeout 须大于 0，limit 须在 1–10000 之间")
    cases = [] if args.list_bases else load_cases(args.cases)
    if args.limit:
        cases = cases[: args.limit]
    if args.export_template:
        args.export_template.parent.mkdir(parents=True, exist_ok=True)
        if args.export_template.exists():
            raise ValueError("模板目标已存在，请使用新文件名，避免覆盖人工标注")
        args.export_template.write_text(
            json.dumps({"cases": cases}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"已导出 {len(cases)} 题；请按文档说明添加 relevant 原文证据：{args.export_template}"
        )
        return 0
    values = dotenv_values(args.env_file)
    api = values.get("RAG_TEST_API_URL", "") or ""
    base = args.base or values.get("RAG_TEST_KNOWLEDGE_BASE_ID")
    account = values.get("RAG_TEST_ACCOUNT")
    origin = values.get("RAG_TEST_ORIGIN")
    endpoint = urlsplit(api)
    if (
        endpoint.scheme not in {"http", "https"}
        or not endpoint.hostname
        or endpoint.username
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("请在评测配置中填写有效 RAG_TEST_API_URL，不允许嵌入凭证")
    if not account or not origin or (not base and not args.list_bases):
        raise ValueError(
            "评测配置缺少账号、Origin 或知识库 ID，参考 docs/evaluation/RETRIEVAL_EVALUATION.md"
        )
    if base and not re.fullmatch(r"[a-zA-Z0-9-]{1,80}", base):
        raise ValueError("知识库 ID 格式不正确")
    password = values.get("RAG_TEST_PASSWORD") or getpass.getpass("知识库登录密码：")
    with httpx.Client(
        base_url=api.rstrip("/"), timeout=args.timeout, trust_env=False
    ) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"email": account, "password": password},
            headers={"Origin": origin},
        )
        response.raise_for_status()
        headers = {"Origin": origin, "X-CSRF-Token": response.json()["csrf_token"]}
        if args.list_bases:
            cursor = None
            seen = set()
            while True:
                response = client.get(
                    "/api/v1/me/knowledge-bases",
                    params={"limit": 100, **({"cursor": cursor} if cursor else {})},
                )
                response.raise_for_status()
                data = response.json()
                for item in data["items"]:
                    print(item["id"], item["name"])
                cursor = data.get("next_cursor")
                if not cursor:
                    return 0
                if cursor in seen:
                    raise ValueError("知识库分页游标重复")
                seen.add(cursor)
        rows = run_cases(client, headers, str(base), cases, args.k)
    report = {
        "created_at": datetime.now().astimezone().isoformat(),
        "knowledge_base_id": base,
        "dataset": str(args.cases.resolve()),
        "scope": "search_api_final_evidence",
        "summary": aggregate(rows, args.k),
        "cases": rows,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    name = "retrieval-" + datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S-%f")
    (args.output / f"{name}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary_path = args.output / f"{name}.md"
    summary = format_report(report)
    summary_path.write_text(summary, encoding="utf-8")
    print(summary)
    print(f"报告：{summary_path}")
    return 1 if report["summary"]["errors"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except httpx.HTTPStatusError as error:
        print(
            f"请求失败：HTTP {error.response.status_code}。检查服务、账号、Origin 和限流状态。",
            file=sys.stderr,
        )
        sys.exit(1)
    except httpx.HTTPError as error:
        print(
            f"服务连接失败：{type(error).__name__}。请先启动开发服务。", file=sys.stderr
        )
        sys.exit(1)
    except (ValueError, TypeError, OSError, KeyError) as error:
        print(f"评测未完成：{error}", file=sys.stderr)
        sys.exit(1)

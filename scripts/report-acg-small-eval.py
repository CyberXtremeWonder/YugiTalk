"""Summarize fixed small-corpus QA and page-level evidence coverage."""

import json
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import unicodedata

from app.rag.parser import parse

OUT = ROOT / "outputs/acg_simpleqa_small_20261029"


def norm(text):
    return unicodedata.normalize("NFKC", text).replace("_", " ").strip()


def main():
    state = json.loads((OUT / "import.json").read_text())
    corpus = json.loads((OUT / "corpus_manifest.json").read_text(encoding="utf-8"))
    rows = {}
    for line in (OUT / "results.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        rows[row["index"]] = row
    assert len(rows) == 200, "200题尚未完成，保留部分结果报告，不计算最终准确率"
    # Reproduce deterministic chunk IDs from unchanged v2 parsing; do not inspect answers.
    chunk_pages, ordinal = {}, 0
    with (OUT / "corpus.jsonl").open("rb") as stream:
        for raw in stream:
            title = norm(json.loads(raw)["title"])
            for part in parse(raw, ".jsonl", "auto"):
                ordinal += 1
                chunk_pages[
                    str(uuid5(NAMESPACE_URL, f"{state['document_id']}:1:{ordinal}"))
                ] = title
    assert ordinal == state["document"]["progress"]["total_chunks"]
    covered = set(corpus["target_titles"])
    hit, complete_pages = 0, 0
    for row in rows.values():
        expected = {norm(unquote(urlsplit(u).path).lstrip("/")) for u in row["urls"]}
        actual = {
            chunk_pages.get(s.get("chunk_id"), "") for s in row.get("sources", [])
        }
        row["reference_pages_present"] = expected <= covered
        row["reference_page_hit"] = bool(expected & actual)
        row["reference_pages_all_returned"] = expected <= actual
        hit += row["reference_page_hit"]
        complete_pages += row["reference_pages_all_returned"]
    grades = Counter(r["grade"] for r in rows.values())
    covered_rows = [r for r in rows.values() if r["reference_pages_present"]]
    result = {
        "total": len(rows),
        "expected_total": 200,
        "grades": dict(grades),
        "accuracy": grades["CORRECT"] / 200,
        "reference_page_hits": hit,
        "all_reference_pages_returned": complete_pages,
        "covered_questions": len(covered_rows),
        "accuracy_on_covered_questions": sum(
            r["grade"] == "CORRECT" for r in covered_rows
        )
        / len(covered_rows),
        "corpus_bytes": corpus["bytes"],
        "chunks": ordinal,
        "raw_dense_vector_bytes": ordinal * 1024 * 4,
        "errors": [
            {"index": r["index"], "error": r.get("error")}
            for r in rows.values()
            if r["grade"] == "ERROR"
        ],
    }
    (OUT / "analysis.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT / "analyzed_results.jsonl").write_text(
        "".join(json.dumps(rows[i], ensure_ascii=False) + "\n" for i in sorted(rows)),
        encoding="utf-8",
    )
    summary = json.loads((OUT / "summary.json").read_text(encoding="utf-8"))
    lines = [
        "# ACG-SimpleQA 200题小语料评测",
        "",
        "固定修订 "
        + summary["dataset"]["revision"]
        + "；种子20261029，每类40题，共200题。",
        "",
        f"知识库 {state['knowledge_base_id']}；真实鉴权及完整指定知识库 RAG API。回答模型与评分模型见 summary.json，评分模板为官方 SimpleQA 三分类模板。",
        "",
        f"语料：194个匹配参考词条＋331个标题相近干扰词条，共525条，{corpus['bytes']}字节。没有导入测试题或参考答案，没有按答案位置剪裁原文。",
        "",
        "## 结果",
        "",
        "| 指标 | 结果 |\n| --- | ---: |",
        f"| 完成题数 | {len(rows)}/200 |",
        f"| 正确 | {grades['CORRECT']} |",
        f"| 错误回答 | {grades['INCORRECT']} |",
        f"| 未作答（含等待澄清） | {grades['NOT_ATTEMPTED']} |",
        f"| 执行/评分错误 | {grades['ERROR']} |",
        f"| 总准确率 | {result['accuracy']:.2%} |",
        f"| 参考页面均匹配子集准确率（{len(covered_rows)}题） | {result['accuracy_on_covered_questions']:.2%} |",
        f"| 最终证据参考页面命中 | {hit}/200 |",
        f"| 子块/向量数 | {ordinal} |",
        f"| float32稠密向量原始体积（不含索引及日志） | {ordinal*4096/1048576:.2f} MiB |",
        "",
        "各类别：",
        "",
        "| 类别 | 正确 | 错答 | 未作答 | 错误 | 准确率 |\n| --- | ---: | ---: | ---: | ---: | ---: |",
        *[f"| {name} | {item.get('CORRECT',0)} | {item.get('INCORRECT',0)} | {item.get('NOT_ATTEMPTED',0)} | {item.get('ERROR',0)} | {item['accuracy']:.2%} |"
          for name,item in summary['categories'].items()],
        "",
        f"单题RAG耗时中位数：{summary['median_rag_seconds']:.2f}秒。",
        "",
        "## 口径与限制",
        "",
        "- 主准确率分母固定200，包含5题未精确匹配参考页的题目；另列195题参考页匹配子集成绩，不用子集成绩替代总成绩。未匹配不等于其他词条中绝无相关事实。",
        "- 页面召回按来源chunk_id映射原始词条，与题目urls匹配。它只是页面级命中，不证明返回片段包含答案，也不是完整引文Recall@5。",
        "- 来源仅含最终保留证据；被评分器清空的候选在此指标中算未命中。",
        "- 这是小语料、固定知识库的端到端问答评测，不代表全百科干扰规模，也不测Agent自动选库。",
        "- 同一模型负责回答和评分可能存在评分偏差，错误及未作答分类仍建议人工抽查。",
        "- 统一最终评分禁用 deepseek-flash 内部推理，以获得有效 A/B/C 响应；所有已保存预测使用同一模板重新评分，没有重新生成答案。等待澄清且无答案计未作答，取消任务只用于释放并发名额。",
        "- 预检发现的并发名额未释放和内部上下文重复计量问题已修复；初轮失败记录独立保存，不当作正式准确率。分块、检索、证据评分标准没有据测试成绩调整。",
        "- 未为提高成绩修改分块/召回/评分阈值或挑掉缺失页面的题目。GraphBuild仅针对本测试文档禁用，其他知识库不变。",
        "- 后续改动必须使用相同冻结题目和语料，另建结果目录；本次样本若用于调参，不再作为独立未见测试集。",
        "",
        "逐题原始结果 results.jsonl；追加页面指标 analyzed_results.jsonl；汇总 summary.json/analysis.json；语料与配置清单 corpus_manifest.json/dataset_manifest.json/evaluation_config.json。",
    ]
    (OUT / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()

"""手动运行 LangSmith 数据集评测，不在导入时调用外部服务。"""

import os
from pathlib import Path
import csv
from functools import partial
from dotenv import load_dotenv

from rag_eval_adapter import call_rag, config, rag_session
from langsmith import evaluate, Client

dataset_name = "RAG"

def add_examples(file_path):
    examples = []
    with open(file_path, "r", encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            examples.append({
                "inputs": {"question": row["question"]},
                "outputs": {"answer": row["answer"]},
            })

    if not os.getenv("LANGSMITH_API_KEY", "").strip():
        raise ValueError("请在 backend/.env.rag-test 中配置 LANGSMITH_API_KEY")
    client = Client()
    dataset = client.read_dataset(dataset_name=dataset_name)

    # Add examples to the dataset
    client.create_examples(dataset_id=dataset.id, examples=examples)

def target_function(inputs: dict, rag=call_rag) -> dict:
    """将数据集的问题传给项目完整 RAG 接口。"""
    question = inputs.get("question", inputs.get("问题"))
    if not isinstance(question, str) or not question.strip():
        raise ValueError(
            f"数据集 Inputs 需要非空字符串字段 question 或 问题，实际字段：{list(inputs)}"
        )
    return rag(question)


def exact_match(outputs: dict, reference_outputs: dict, run=None) -> dict:
    """比较固定答案字段，自由生成答案应另行采用语义和证据评分。"""
    if run is not None and run.error:
        return {
            "key": "exact_match", "score": None,
            "comment": "目标调用失败，未评分。请查看该条运行的 Error。",
        }
    answer = (outputs or {}).get("answer")
    reference = (reference_outputs or {}).get("answer")
    if not isinstance(answer, str) or not isinstance(reference, str):
        return {
            "key": "exact_match", "score": None,
            "comment": (
                "实际输出和参考输出均需要字符串字段 answer。"
                f"实际字段：{list(outputs or {})}；参考字段：{list(reference_outputs or {})}"
            ),
        }
    return {"key": "exact_match", "score": answer.strip() == reference.strip()}


def main() -> None:
    """确认外部上传后按单并发运行数据集，控制模型请求数量。"""
    load_dotenv(Path(__file__).with_name(".env.rag-test"))
    for name in (
        "LANGSMITH_API_KEY", "RAG_TEST_API_URL", "RAG_TEST_ORIGIN",
        "RAG_TEST_ACCOUNT", "RAG_TEST_PASSWORD", "RAG_TEST_AGENT_VERSION_ID",
        "RAG_TEST_KNOWLEDGE_BASE_ID",
    ):
        config(name)
    from langsmith import evaluate

    with rag_session() as rag:
        evaluate(
            partial(target_function, rag=rag),
            data=dataset_name,
            evaluators=[exact_match],
            experiment_prefix="YugiTalk-RAG experiment",
            max_concurrency=1,
            blocking=True,
        )


if __name__ == "__main__":
    # load_dotenv(Path(__file__).with_name(".env.rag-test"))
    # add_examples(
    #     Path(__file__).resolve().parents[1]
    #     / "outputs" / "agent_memory_qa" / "agent_memory_50_qa.csv"
    # )
    main()

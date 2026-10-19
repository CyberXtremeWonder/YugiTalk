"""验证训练段落分包、来源标注和数据冲突保护，不连接实际知识库。"""

import importlib.util
import json
from pathlib import Path

import pytest


def load_importer():
    """加载受主入口保护的导入脚本，不触发下载或上传。"""
    path = Path(__file__).resolve().parents[2] / "scripts/import-miracl-training.py"
    spec = importlib.util.spec_from_file_location("miracl_importer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("split", ["train", "dev"])
def test_packages_keep_both_labels_and_do_not_index_questions(tmp_path, split):
    """相同段落只导入一次，负例保留，问题只存在于评测文件中。"""
    importer = load_importer()
    positive = {"docid": "p1", "title": "标题一", "text": "正确原文"}
    negative = {"docid": "p2", "title": "标题二", "text": "干扰原文"}
    rows = [
        {
            "query_id": "q1",
            "query": "不应上传的问题文本",
            "positive_passages": [positive],
            "negative_passages": [negative],
        },
        {
            "query_id": "q2",
            "query": "另一个问题",
            "positive_passages": [positive],
            "negative_passages": [],
        },
    ]
    result = importer.prepare(rows, tmp_path, batch_size=1, split=split)
    assert result["questions"] == 2 and result["passages"] == 2
    assert len(result["documents"]) == 2
    assert all(item["filename"].startswith(f"miracl-zh-{split}-") for item in result["documents"])
    content = "".join(
        path.read_text(encoding="utf-8") for path in (tmp_path / "documents").glob("*.md")
    )
    assert "干扰原文" in content and "不应上传的问题文本" not in content
    cases = json.loads((tmp_path / "cases.json").read_text(encoding="utf-8"))["cases"]
    assert cases[0]["positive_passage_ids"] == ["p1"]
    assert cases[0]["relevant"][0]["quote"] == "正确原文"


def test_conflicting_passage_ids_fail_before_import(tmp_path):
    """禁止同一原始段落 ID 被静默覆盖为不同内容。"""
    rows = [
        {
            "query_id": "q1",
            "query": "问题",
            "positive_passages": [{"docid": "same", "title": "标题", "text": "第一版"}],
            "negative_passages": [{"docid": "same", "title": "标题", "text": "第二版"}],
        }
    ]
    with pytest.raises(ValueError, match="不同内容"):
        load_importer().prepare(rows, tmp_path)


def test_identical_positive_text_keeps_distinct_passage_identities(tmp_path):
    """同包内原文相同但 ID 不同的正例保留两条标注，以原始标记区分。"""
    rows = [
        {
            "query_id": "q1",
            "query": "问题",
            "positive_passages": [
                {"docid": "p1", "title": "标题一", "text": "相同原文"},
                {"docid": "p2", "title": "标题二", "text": "相同原文"},
            ],
            "negative_passages": [],
        }
    ]
    load_importer().prepare(rows, tmp_path, split="dev")
    case = json.loads((tmp_path / "cases.json").read_text(encoding="utf-8"))["cases"][0]
    assert case["positive_passage_ids"] == ["p1", "p2"]
    assert [item["quote"] for item in case["relevant"]] == [
        "MIRACL_DOCID: p1\n相同原文",
        "MIRACL_DOCID: p2\n相同原文",
    ]

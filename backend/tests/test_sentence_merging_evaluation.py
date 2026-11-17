"""验证实验仅合并短句，保留原文范围、token 上限和父块身份。"""

import copy
import importlib.util
from pathlib import Path

import pytest
from tokenizers import Tokenizer, models, pre_tokenizers, processors

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "sentence_merging", ROOT / "scripts/evaluate-sentence-merging.py"
)
merging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merging)


@pytest.fixture
def tokenizer():
    """每字符一 token，包含两个特殊 token，方便核对句子边界。"""
    result = Tokenizer(models.WordLevel({"[UNK]": 0, "[CLS]": 1, "[SEP]": 2}, unk_token="[UNK]"))
    result.pre_tokenizer = pre_tokenizers.Split("", behavior="isolated")
    result.post_processor = processors.TemplateProcessing(
        single="[CLS] $A [SEP]", special_tokens=[("[CLS]", 1), ("[SEP]", 2)]
    )
    return result


def test_merge_complete_sentences_to_target(tokenizer):
    """三个短句合并达到 200–256，余句不跨父块凑满。"""
    sentence = "甲" * 69 + "。"
    parent = sentence * 5
    spans = list(merging.merged_spans(parent, tokenizer))
    assert [parent[start:end] for start, end in spans] == [sentence * 3, sentence * 2]
    assert 200 <= len(tokenizer.encode(parent[spans[0][0] : spans[0][1]]).ids) <= 256
    assert "".join(parent[start:end] for start, end in spans) == parent
    from app.rag.chunking import merged_sentence_spans
    assert list(merged_sentence_spans(parent, tokenizer)) == spans


@pytest.mark.parametrize(
    "text", ["短文。", "甲" * 260 + "。乙。", "甲" * 150 + "。" + "乙" * 150 + "。"]
)
def test_limits_and_complete_coverage(tokenizer, text):
    """短父块保留，超长句切分和大句边界均不丢失字符。"""
    spans = list(merging.merged_spans(text, tokenizer))
    covered = [False] * len(text)
    for start, end in spans:
        assert len(tokenizer.encode(text[start:end].strip()).ids) <= 256
        for index in range(start, end):
            covered[index] = True
    assert all(covered)
    if len(text) < 200:
        assert spans == [(0, len(text))]


def test_parent_identity_and_input_unchanged(tokenizer):
    """不同父块不混合，父块原文和页码保持，输入对照不被修改。"""
    doc = {
        "id": "doc",
        "parts": [
            {
                "parent_group": 1,
                "parent_content": "甲。" * 150,
                "page": 1,
                "profile": "structured",
                "strategy_version": "pc-auto-structured-v1",
            },
            {
                "parent_group": 2,
                "parent_content": "乙。" * 150,
                "page": 2,
                "profile": "structured",
                "strategy_version": "pc-auto-structured-v1",
            },
        ],
    }
    before = copy.deepcopy(doc)
    result = merging.merge_document(doc, tokenizer)
    assert doc == before
    assert len([p for p in result["parts"] if "parent_content" in p]) == 2
    assert all(not ("甲" in p["content"] and "乙" in p["content"]) for p in result["parts"])
    assert [p["ordinal"] for p in result["parts"]] == list(range(1, len(result["parts"]) + 1))
    assert merging.merge_document(doc, tokenizer) == result

"""验证实验切分保留原文、遵守 token 上限且与字符基线一致。"""

import importlib.util
from pathlib import Path

import pytest

from app.rag.parser import parse

pytest.importorskip("tokenizers", reason="分块实验需单独安装 tokenizers")

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "token_benchmark", ROOT / "scripts/benchmark-token-chunking.py"
)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


@pytest.fixture
def tokenizer():
    """构造含特殊 token 的小型分词器，不依赖模型下载。"""
    from tokenizers import Tokenizer, models, pre_tokenizers, processors

    tokenizer = Tokenizer(
        models.WordLevel(
            {"[UNK]": 0, "[CLS]": 1, "[SEP]": 2, "hello": 3, "world": 4}, unk_token="[UNK]"
        )
    )
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.post_processor = processors.TemplateProcessing(
        single="[CLS] $A [SEP]", special_tokens=[("[CLS]", 1), ("[SEP]", 2)]
    )
    return tokenizer


@pytest.mark.parametrize("size", [512, 256, 128, 100])
def test_token_windows_preserve_text_and_budget(tokenizer, size):
    """每块独立编码仍不超限，偏移连续覆盖全文，最后无重复尾块。"""
    text = "hello world 中文 👩‍💻 café\n" * 600
    parts = list(benchmark.split_tokens(text, tokenizer, size))
    assert parts[0][0] == 0 and parts[-1][1] == len(text)
    for start, end, content in parts:
        assert content == text[start:end].strip()
        assert len(tokenizer.encode(content).ids) <= size
    assert all(a[0] < b[0] <= a[1] for a, b in zip(parts, parts[1:], strict=False))
    assert len(parts) > 1


@pytest.mark.parametrize("size,overlap", [(512, 51), (256, 26), (128, 13), (100, 10)])
def test_ten_percent_overlap(tokenizer, size, overlap):
    """10% 按最近整数取整，特殊 token 不参与正文交叠。"""
    text = "hello " * 2000
    parts = list(benchmark.split_tokens(text, tokenizer, size))
    for previous, current in zip(parts, parts[1:], strict=False):
        shared = text[current[0] : previous[1]]
        assert len(tokenizer.encode(shared, add_special_tokens=False).ids) == overlap


@pytest.mark.parametrize(
    "strategy,production", [("fixed700", "fixed"), ("sections700", "markdown_sections")]
)
def test_character_baselines_match_parser(tokenizer, strategy, production):
    """字符基线必须与现有解析器完全一致。"""
    text = "开场\n## 第一节\n" + "甲乙丙。\n" * 410 + "\n## 第二节\n短内容"
    leaves, _ = benchmark.make_chunks([{"id": "doc", "text": text}], strategy, tokenizer)
    assert [leaf["content"] for leaf in leaves] == [
        part["content"] for part in parse(text.encode(), ".md", production)
    ]


def test_parent_ranges_and_section_isolation(tokenizer):
    """父块来自同一小节原文，不重复插入重叠，也不混入相邻标题。"""
    first = "## A\n" + "hello world " * 300
    second = "## B\nshort"
    leaves, parents = benchmark.make_chunks(
        [{"id": "doc", "text": first + "\n" + second}], "token100", tokenizer
    )
    assert all(
        parent["content"] in first or parent["content"] in second for parent in parents.values()
    )
    assert all(parent["child_count"] <= 3 for parent in parents.values())
    assert leaves[-1]["content"] == second


def test_strict_evidence_score():
    """部分引文不能冒充完整证据，跨文档同文不计命中。"""
    target = [{"document_id": "a", "quote": "完整证据"}]
    assert benchmark.score(target, [{"document_id": "a", "content": "完整"}])["recall"] == 0
    assert benchmark.score(target, [{"document_id": "b", "content": "完整证据"}])["recall"] == 0
    assert benchmark.score(target, [{"document_id": "a", "content": "完整 证据"}])["recall"] == 1


def test_unchanged_chunks_keep_tie_break_identity(tokenizer):
    """同一原文块跨方案保持身份，避免 RRF 并列顺序引入实验噪声。"""
    documents = [{"id": "doc", "text": "## A\nhello world"}]
    groups = [
        benchmark.make_chunks(documents, mode, tokenizer)[0]
        for mode in ("sections700", "sections700o10", "token512", "token100")
    ]
    assert len({leaves[0]["id"] for leaves in groups}) == 1

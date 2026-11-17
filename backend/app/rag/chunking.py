"""按文档类型先生成父块，再在原文范围内生成任意数量的重叠子块。"""

import re
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from app.core.config import PROJECT_ROOT, Settings
from app.rag.limits import MAX_DOCUMENT_CHUNKS

ChunkStrategy = Literal[
    "auto", "short", "long", "tokens", "structured", "ultra", "fixed", "markdown_sections"
]
PROFILES = {
    "short": (500, 100, "characters"),
    "long": (1000, 200, "characters"),
    "tokens": (1024, 256, "tokens"),
    "structured": (2048, 256, "tokens"),
    "ultra": (4096, 400, "tokens"),
}
SECTION = re.compile(
    r"(?m)^(?:#{1,6}[ \t]+[^\n]+|【[^\n】]+】[^\n]*|第[一二三四五六七八九十百\d]+[章节][^\n]*)$"
)


def strategy_version(strategy: str, resolved: str | None = None) -> str:
    """持久化请求方式与识别结果，重试时不丢失用户选择。"""
    if strategy == "fixed":
        return "paragraph-700-overlap100-v1"
    if strategy == "markdown_sections":
        return "markdown-sections-700-parent-v1"
    return f"pc-{strategy}-{resolved or strategy}-v1"


def requested_strategy(version: str) -> str:
    """读取新旧策略标识，拒绝未知的新版本而非静默切换规则。"""
    if version.startswith("pc-"):
        parts = version.split("-")
        if len(parts) != 4 or parts[1] not in {"auto", *PROFILES} or parts[3] != "v1":
            raise ValueError("document_chunk_strategy_invalid")
        return parts[1]
    return "markdown_sections" if version == "markdown-sections-700-parent-v1" else "fixed"


def detect_profile(text: str) -> str:
    """先识别章节/条目，再按长度与语言选择规则；不调用生成模型。"""
    visible = re.sub(r"(?ms)^```.*?^```[^\n]*", "", text)
    headings = len(list(SECTION.finditer(visible)))
    items = len(re.findall(r"(?m)^\s*(?:\d+[.、)]|[一二三四五六七八九十]+、)\s*\S", visible))
    if headings >= 2 or items >= 3:
        return "structured"
    if len(text) >= 20000:
        return "ultra"
    if len(text) > 2000:
        letters = sum(character.isascii() and character.isalpha() for character in text)
        return "tokens" if letters > len(text) / 2 else "long"
    return "short"


@lru_cache(maxsize=2)
def load_tokenizer(model_path: str, revision: str) -> Any:
    """只加载本地 BGE-M3 tokenizer，不下载权重，不使用字符数冒充 token。"""
    from tokenizers import Tokenizer

    root = Path(model_path)
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    if root.is_dir():
        direct = root / "tokenizer.json"
        root = direct if direct.is_file() else root / "snapshots" / revision / "tokenizer.json"
    try:
        tokenizer = Tokenizer.from_file(str(root))
    except Exception as error:
        raise ValueError("chunk_tokenizer_unavailable") from error
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return tokenizer


def windows(
    text: str,
    limit: int,
    unit: str,
    overlap: int = 0,
    tokenizer: Any = None,
    max_characters: int | None = None,
) -> Iterator[tuple[int, int]]:
    """优先在段落和句末结束；token 使用偏移回切原文并重新校验上限。"""
    start = 0
    while start < len(text):
        rest = text[start:]
        capacity = limit - tokenizer.num_special_tokens_to_add(False) if tokenizer else limit
        encoding = None
        if tokenizer:
            # 只编码足以覆盖当前窗口的前缀，避免长文每切一块都重新编码全部剩余文本。
            prefix_size = min(len(rest), max(1, limit * 2))
            encoding = tokenizer.encode(rest[:prefix_size], add_special_tokens=False)
            while len(encoding.ids) <= capacity and prefix_size < len(rest):
                prefix_size = min(len(rest), prefix_size * 2)
                encoding = tokenizer.encode(rest[:prefix_size], add_special_tokens=False)
        end = (
            encoding.offsets[capacity - 1][1]
            if encoding and len(encoding.ids) > capacity
            else len(rest)
            if encoding
            else min(limit, len(rest))
        )
        if max_characters is not None:
            end = min(end, max_characters)
        if end < len(rest):
            boundaries = list(
                re.finditer(r"\n\s*\n|[。！？!?；;](?:[”’\"']|\s)*|[.!?](?=\s)", rest[:end])
            )
            if boundaries and boundaries[-1].end() >= end // 2:
                end = boundaries[-1].end()
        if tokenizer:
            while len(tokenizer.encode(rest[:end].strip()).ids) > limit:
                end -= 1
        if end <= 0:
            raise ValueError("document_chunk_strategy_invalid")
        if rest[:end].strip():
            yield start, start + end
        if end == len(rest):
            break
        next_start = end
        if overlap:
            if tokenizer:
                previous = tokenizer.encode(rest[:end], add_special_tokens=False)
                next_start = previous.offsets[max(0, len(previous.ids) - overlap)][0]
            else:
                next_start = max(1, end - overlap)
        start += max(1, next_start)


def merged_sentence_spans(parent: str, tokenizer: Any) -> Iterator[tuple[int, int]]:
    """同一父块内合并完整短句，目标 200 tokens，上限 256 tokens/2000 字符。"""
    ends = [0, *(m.end() for m in re.finditer(
        r"[。！？!?](?:[”’\"']|\s)*|(?m:\n(?=\s*(?:[-*+] |\d+[.、)])))", parent,
    )), len(parent)]
    start, finish = None, 0
    for begin, end in zip(ends, ends[1:], strict=False):
        sentence = parent[begin:end]
        if not sentence.strip():
            continue
        if len(tokenizer.encode(sentence.strip()).ids) > 256 or len(sentence.strip()) > 2000:
            if start is not None:
                yield start, finish
                start = None
            for first, last in windows(sentence, 256, "tokens", 26, tokenizer, 2000):
                yield begin + first, begin + last
            continue
        if start is not None and (
            len(tokenizer.encode(parent[start:end].strip()).ids) > 256
            or len(parent[start:end].strip()) > 2000
        ):
            yield start, finish
            start = None
        if start is None:
            start = begin
        finish = end
        if len(tokenizer.encode(parent[start:finish].strip()).ids) >= 200:
            yield start, finish
            start = None
    if start is not None:
        yield start, finish


def chunk_pages(
    pages: list[tuple[int | None, str]], strategy: str, *, merge_short_sentences: bool = False,
) -> list[dict[str, Any]]:
    """所有父块保留来源页和主题边界；子块数量由长度决定。"""
    profile = (
        detect_profile("\n".join(text for _, text in pages)) if strategy == "auto" else strategy
    )
    if merge_short_sentences and strategy == "auto":
        profile = "structured"
    parent_size, child_size, unit = PROFILES[profile]
    tokenizer = None
    if unit == "tokens":
        settings = Settings()
        if settings.embedding_model != "BAAI/bge-m3":
            raise ValueError("chunk_tokenizer_model_mismatch")
        tokenizer = load_tokenizer(settings.bge_model_path, settings.embedding_revision)
    result: list[dict[str, Any]] = []
    parent_index = 0
    for page, raw in pages:
        text = raw.strip()
        positions = {0, len(text), *(m.start() for m in SECTION.finditer(text))}
        if profile == "structured":
            positions.update(
                m.start()
                for m in re.finditer(r"(?m)^(?:\d+[.、)]|[一二三四五六七八九十]+、)\s*\S", text)
            )
        boundaries = sorted(positions)
        for begin, finish in zip(boundaries, boundaries[1:], strict=False):
            section = text[begin:finish].strip()
            for p_start, p_end in windows(section, parent_size, unit, tokenizer=tokenizer):
                parent = section[p_start:p_end].strip()
                parent_index += 1
                if profile == "structured":
                    # 完整子条目/句子优先；过长句子仍受子块上限保护。
                    child_boundaries = [
                        0,
                        *(
                            m.end()
                            for m in re.finditer(
                                r"[。！？!?](?:[”’\"']|\s)*|(?m:\n(?=\s*(?:[-*+] |\d+[.、)])))",
                                parent,
                            )
                        ),
                        len(parent),
                    ]
                else:
                    child_boundaries = [0, len(parent)]
                spans = (
                    merged_sentence_spans(parent, tokenizer)
                    if merge_short_sentences and profile == "structured"
                    else zip(child_boundaries, child_boundaries[1:], strict=False)
                )
                for c_begin, c_end in spans:
                    body = parent[c_begin:c_end]
                    for start, end in windows(
                        body,
                        child_size,
                        unit,
                        round(child_size * 0.1),
                        tokenizer,
                        max_characters=2000,
                    ):
                        first_child = not result or result[-1]["parent_group"] != parent_index
                        result.append(
                            {
                                "page": page,
                                "ordinal": len(result) + 1,
                                "content": body[start:end].strip(),
                                "parent_group": parent_index,
                                **({"parent_content": parent} if first_child else {}),
                                "profile": profile,
                                "strategy_version": strategy_version(strategy, profile),
                            }
                        )
                        if len(result) > MAX_DOCUMENT_CHUNKS:
                            raise ValueError("too_many_chunks")
    if not result:
        raise ValueError("empty_document")
    return result

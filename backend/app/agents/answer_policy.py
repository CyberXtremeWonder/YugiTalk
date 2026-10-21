"""将检索覆盖程度转换为生成约束，并校验模型使用的来源身份。"""

import json
import re
from typing import Any

from app.providers.client import ProviderFailure


def evidence_policy(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """只读取程序生成的工具结果或 RAG 上下文，不把用户文本当成授权来源。"""
    results = []
    for message in messages:
        if message.get("role") == "tool":
            try:
                value = json.loads(message.get("content", ""))
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and "coverage" in value:
                results.append(value)
        elif message.get("rag_evidence") is not None:
            results.append(message["rag_evidence"])
    if not results:
        return None
    sources = {
        source["evidence_id"]: source
        for result in results for source in result.get("sources", [])
    }
    # 多库查询允许相互补充；无结果的某一库不能清除另一库已验证的证据。
    coverage = "sufficient" if any(
        result.get("coverage") == "sufficient" for result in results
    ) else "partial" if sources else "none"
    return {"coverage": coverage, "sources": list(sources.values())}


def answer_instruction(policy: dict[str, Any]) -> str:
    """三个覆盖分支互斥，空来源不会继续携带要求引用资料的生成指令。"""
    common = "来源由应用卡片展示，不输出来源链接或 [S1] 等索引，不编造 evidence_id。"
    if policy["coverage"] == "none":
        return (
            "本次检索无有效证据。仅简短说明检索限制，不声称文件不存在，"
            "不编造资料结论，不以通用长文替代知识库回答。" + common
        )
    if policy["coverage"] == "partial":
        return (
            "资料仅部分支持问题。只回答有原文支持的部分，明确标注未覆盖内容，不扩展主题。"
            + common
        )
    return "仅依据有效资料回答当前问题，保持简洁。" + common


def validate_references(text: str, policy: dict[str, Any]) -> None:
    """引用身份必须属于本次证据包；卡片展示模式禁止模型自行生成编号引用。"""
    allowed = {source["evidence_id"] for source in policy["sources"]}
    # 中文与英文字母都属于正则的“单词字符”，不能用 \b 判断中文旁边的证据 ID。
    references = set(re.findall(r"(?<![a-zA-Z0-9_])E-[a-zA-Z0-9_-]+", text))
    if references - allowed or re.search(r"\[\s*S\d+\s*\]", text):
        raise ProviderFailure("invalid_citation")

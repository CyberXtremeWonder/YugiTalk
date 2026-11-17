"""保存解析器生成的独立父块，并兼容旧版三子一父关系。"""

from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy.orm import Session

from app.rag.models import ParentChunk


def join_overlaps(texts: list[str]) -> str:
    """消除解析器产生的相邻重叠部分；无重叠时保留分段边界。"""
    result = texts[0] if texts else ""
    for text in texts[1:]:
        # 从最长重叠向下寻找前块尾部与后块开头的公共部分，仅去掉真实重复的文字。
        overlap = next(
            (
                size
                for size in range(min(100, len(result), len(text)), 0, -1)
                if result.endswith(text[:size])
            ),
            0,
        )
        result += text[overlap:] if overlap else "\n" + text
    return result


def attach_parents(
    db: Session, document: str, version: int, parts: list[dict[str, Any]],
    ordinal_offset: int = 0,
) -> None:
    """写入确定性父级身份，并给每个叶子标注经过校验的父级关系。"""
    groups: list[list[dict[str, Any]]] = []
    for part in parts:
        # 新策略使用解析时生成的父块身份；旧策略沿用最多三子一父。
        if (
            not groups
            or groups[-1][0]["page"] != part["page"]
            or (
                groups[-1][0].get("parent_group") != part.get("parent_group")
                if "parent_group" in part
                else len(groups[-1]) == 3 or groups[-1][0].get("section") != part.get("section")
            )
        ):
            groups.append([])
        groups[-1].append(part)
    for ordinal, group in enumerate(groups, ordinal_offset + 1):
        parent_id = str(uuid5(NAMESPACE_URL, f"{document}:{version}:parent:{ordinal}"))
        db.add(
            ParentChunk(
                id=parent_id,
                document_id=document,
                index_version=version,
                ordinal=ordinal,
                page=group[0]["page"],
                content=(
                    group[0]["parent_content"]
                    if "parent_group" in group[0]
                    else join_overlaps([part["content"] for part in group])
                ),
                child_count=len(group),
            )
        )
        for part in group:
            part["parent_id"] = parent_id
    db.flush()

"""有界游标分页参数与列表响应。"""

from typing import Annotated

from fastapi import Query

Limit = Annotated[int, Query(ge=1, le=100)]


def page(items: list[dict[str, object]], limit: int) -> dict[str, object]:
    """截取有界列表并生成下一页游标，保留额外一条作为判断依据。"""
    return {
        "items": items[:limit],
        "next_cursor": items[limit - 1]["id"] if len(items) > limit else None,
    }

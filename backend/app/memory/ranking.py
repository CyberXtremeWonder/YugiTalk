"""在已授权候选集上融合相关性、重要性和时间分数，按完整条目分配预算。"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from app.memory.models import Memory


@dataclass(frozen=True)
class RankedMemory:
    """记录排序依据，追踪只输出 ID 和分数，不复制记忆正文。"""

    memory: Memory
    relevance: float
    importance: float
    recency: float
    score: float


def rank_memories(
    candidates: list[Memory], relevance: list[float], reference_time: datetime,
    weights: tuple[float, float, float], half_life_days: float,
) -> list[RankedMemory]:
    """时间以任务创建时刻为准；SQLite 的无时区时间按数据库约定解释为 UTC。"""
    reference = reference_time
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    ranked = []
    for memory, similarity in zip(candidates, relevance, strict=True):
        created = memory.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        age = max(0.0, (reference - created).total_seconds() / 86400)
        r = max(0.0, min(1.0, similarity))
        importance = (memory.importance - 1) / 4
        recency = 2 ** (-age / half_life_days)
        score = math.fsum(w * v for w, v in zip(weights, (r, importance, recency), strict=True))
        ranked.append(RankedMemory(memory, r, importance, recency, score))
    return sorted(ranked, key=lambda row: (-row.score, -row.relevance, row.memory.id))


def select_memories(ranked: list[RankedMemory], budget_bytes: int = 2000) -> list[RankedMemory]:
    """跳过放不下的完整条目，继续尝试后面的短条目；不截断否定和条件。"""
    selected: list[RankedMemory] = []
    used = 0
    for row in ranked:
        size = len(row.memory.content.encode("utf-8")) + bool(selected)
        if used + size <= budget_bytes:
            selected.append(row)
            used += size
    return selected

"""可选 HTTP 重排：输入有界、绝对截止时间、进程内熔断和明确回退状态。"""

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.core.config import Settings
from app.rag.embeddings import bounded_post


@dataclass
class Breaker:
    """记录同一端点连续失败及唯一半开探测，不跨进程共享可变状态。"""

    failures: int = 0
    open_until: float = 0
    probing: bool = False


_breakers: dict[tuple[str, str], Breaker] = {}
_lock = threading.Lock()


class Reranker:
    """仅对已授权融合候选重排，失败保留原排序，不发起另一套检索。"""

    def __init__(self, settings: Settings) -> None:
        """按端点与模型区分熔断状态，避免一个服务影响其他重排配置。"""
        self.settings = settings
        self.key = (settings.rag_rerank_url, settings.rag_rerank_model)

    async def run(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        deadline: float,
        guard: Callable[[], None],
    ) -> tuple[list[dict[str, Any]], str]:
        """限定数量和总字数；关闭、超时、异常与熔断均返回明确状态。"""
        guard()
        if not self.settings.rag_rerank_url or not candidates:
            return candidates, "disabled" if not self.settings.rag_rerank_url else "empty"
        target = urlsplit(self.settings.rag_rerank_url)
        if (
            target.scheme
            not in ({"http", "https"} if self.settings.app_env != "production" else {"https"})
            or not target.hostname
            or target.username
            or target.query
            or target.fragment
        ):
            raise ValueError("rerank_configuration_invalid")
        with _lock:
            breaker = _breakers.setdefault(self.key, Breaker())
            if breaker.open_until:
                if time.monotonic() < breaker.open_until or breaker.probing:
                    return candidates, "circuit_open"
                breaker.probing = True
        bounded = []
        texts = []
        remaining = self.settings.rag_rerank_max_characters
        for candidate in candidates[: self.settings.rag_candidate_limit]:
            text = candidate["content"][: min(2000, remaining)]
            if not text:
                break
            texts.append(text)
            bounded.append(candidate)
            remaining -= len(text)
        try:
            key = self.settings.rag_rerank_api_key.get_secret_value()
            data = await bounded_post(
                self.settings.rag_rerank_url,
                {
                    "model": self.settings.rag_rerank_model,
                    "query": query[:1000],
                    "documents": texts,
                    "top_n": len(texts),
                },
                {"Authorization": "Bearer " + key} if key else {},
                min(deadline, time.monotonic() + self.settings.rag_rerank_timeout_seconds),
                guard,
            )
            results = data.get("results")
            if not isinstance(results, list) or len(results) != len(bounded):
                raise ValueError("rerank_response_invalid")
            indices: set[int] = set()
            scores = {}
            for row in results:
                index, score = row.get("index"), row.get("relevance_score")
                if (
                    type(index) is not int
                    or not 0 <= index < len(bounded)
                    or index in indices
                    or type(score) not in (float, int)
                    or not math.isfinite(score)
                ):
                    raise ValueError("rerank_response_invalid")
                indices.add(index)
                scores[index] = float(score)
            ranked = [
                {**bounded[index], "rerank_score": scores[index]}
                for index in sorted(scores, key=lambda index: -scores[index])
            ]
            with _lock:
                breaker.failures, breaker.open_until = 0, 0
            return [*ranked, *candidates[len(bounded) :]], "applied"
        except (httpx.HTTPError, ValueError, RuntimeError, TimeoutError):
            with _lock:
                breaker.failures += 1
                if breaker.failures >= self.settings.rag_rerank_failure_threshold:
                    breaker.open_until = (
                        time.monotonic() + self.settings.rag_rerank_cooldown_seconds
                    )
            return candidates, "fallback"
        finally:
            # 取消和权限错误不能被回退吞掉，但半开状态仍必须释放。
            with _lock:
                breaker.probing = False

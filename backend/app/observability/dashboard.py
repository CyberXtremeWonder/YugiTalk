"""实时面板的有界请求聚合与持久任务统计，不采集业务正文。"""

import time
from collections import deque
from datetime import UTC, datetime
from threading import Lock
from typing import Any, cast

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.rag.models import Document
from app.tasks.models import Run


class LiveRequests:
    """按分钟聚合最近十五分钟请求，仅保存二十条异常关联信息。"""

    def __init__(self) -> None:
        """初始化进程内统计，不建立额外连接或后台线程。"""
        self.started_at = datetime.now(UTC).isoformat()
        self.lock = Lock()
        self.buckets: dict[int, dict[tuple[str, str], dict[str, float]]] = {}
        self.errors: deque[dict[str, Any]] = deque(maxlen=20)

    def record(
        self,
        method: str,
        route: str,
        status: int,
        duration_ms: float,
        request_id: str,
        trace_id: str,
        error_code: str | None,
    ) -> None:
        """累计路由请求、错误和耗时，只保留服务端生成的关联标识。"""
        stamp = time.time()
        minute = int(stamp // 60)
        with self.lock:
            self.buckets = {key: value for key, value in self.buckets.items() if key >= minute - 14}
            row = self.buckets.setdefault(minute, {}).setdefault(
                (method, route),
                {
                    "requests": 0,
                    "server_errors": 0,
                    "client_errors": 0,
                    "duration_ms": 0,
                    "max_ms": 0,
                },
            )
            row["requests"] += 1
            row["server_errors"] += int(status >= 500)
            row["client_errors"] += int(400 <= status < 500)
            row["duration_ms"] += duration_ms
            row["max_ms"] = max(row["max_ms"], duration_ms)
            if status >= 400:
                self.errors.append(
                    {
                        "timestamp": datetime.fromtimestamp(stamp, UTC).isoformat(),
                        "kind": "http",
                        "route": route,
                        "method": method,
                        "status": status,
                        "code": error_code
                        or ("server_error" if status >= 500 else "request_error"),
                        "request_id": request_id,
                        "trace_id": trace_id,
                    }
                )

    def snapshot(self, sampled: datetime) -> dict[str, Any]:
        """输出固定数量的趋势桶、接口排行和异常，不暴露原始路径。"""
        minute = int(sampled.timestamp() // 60)
        routes: dict[tuple[str, str], dict[str, float]] = {}
        trend = []
        with self.lock:
            for key in range(minute - 14, minute + 1):
                rows = self.buckets.get(key, {})
                trend.append(
                    {
                        "timestamp": datetime.fromtimestamp(key * 60, UTC).isoformat(),
                        "requests": int(sum(row["requests"] for row in rows.values())),
                        "server_errors": int(sum(row["server_errors"] for row in rows.values())),
                        "average_ms": round(
                            sum(row["duration_ms"] for row in rows.values())
                            / sum(row["requests"] for row in rows.values()),
                            2,
                        )
                        if rows
                        else 0,
                    }
                )
                for route, row in rows.items():
                    total = routes.setdefault(route, {field: 0 for field in row})
                    for field, value in row.items():
                        total[field] = (
                            max(total[field], value)
                            if field == "max_ms"
                            else (total[field] + value)
                        )
            cutoff = datetime.fromtimestamp((minute - 14) * 60, UTC).isoformat()
            errors = [dict(row) for row in reversed(self.errors) if row["timestamp"] >= cutoff]
        count = int(sum(row["requests"] for row in routes.values()))
        server_errors = int(sum(row["server_errors"] for row in routes.values()))
        return {
            "started_at": self.started_at,
            "requests": count,
            "server_errors": server_errors,
            "client_errors": int(sum(row["client_errors"] for row in routes.values())),
            "error_rate": round(server_errors / count * 100, 2) if count else 0,
            "average_ms": round(sum(row["duration_ms"] for row in routes.values()) / count, 2)
            if count
            else 0,
            "trend": trend,
            "errors": errors,
            "routes": [
                {
                    "method": method,
                    "route": route,
                    "requests": int(row["requests"]),
                    "server_errors": int(row["server_errors"]),
                    "client_errors": int(row["client_errors"]),
                    "average_ms": round(row["duration_ms"] / row["requests"], 2),
                    "max_ms": round(row["max_ms"], 2),
                }
                for (method, route), row in sorted(
                    routes.items(), key=lambda item: item[1]["requests"], reverse=True
                )[:20]
            ],
        }


def task_snapshot(db: Session, sampled: datetime) -> dict[str, Any]:
    """聚合全平台已结算用量和任务状态，不返回用户、提示词或资料名称。"""
    minute = int(sampled.timestamp() // 60)
    boundaries = [datetime.fromtimestamp((minute - 14 + offset) * 60, UTC) for offset in range(15)]
    settled = Run.status.in_(["succeeded", "failed", "cancelled"])
    charged, inputs, outputs, unknown = db.execute(
        select(
            func.coalesce(func.sum(Run.charged_tokens), 0),
            func.coalesce(func.sum(Run.input_tokens), 0),
            func.coalesce(func.sum(Run.output_tokens), 0),
            func.coalesce(
                func.sum(
                    case(
                        (settled & (Run.input_tokens.is_(None) | Run.output_tokens.is_(None)), 1),
                        else_=0,
                    )
                ),
                0,
            ),
        )
    ).one()
    bucket = case(
        *[
            (Run.finished_at < boundary, index - 1)
            for index, boundary in enumerate(boundaries)
            if index
        ],
        else_=14,
    )
    recent = db.execute(
        select(bucket, func.sum(Run.charged_tokens))
        .where(
            Run.finished_at >= boundaries[0],
            Run.finished_at <= sampled,
        )
        .group_by(bucket)
    ).all()
    amounts = {int(cast(int, index)): int(cast(int, amount or 0)) for index, amount in recent}
    states = {
        status: int(count)
        for status, count in db.execute(select(Run.status, func.count()).group_by(Run.status))
    }
    failures = [
        {
            "kind": "task",
            "id": key,
            "code": code or "task_failed",
            "timestamp": stamp.replace(tzinfo=UTC).isoformat() if stamp else None,
        }
        for key, code, stamp in db.execute(
            select(Run.id, Run.error_code, Run.finished_at)
            .where(Run.status == "failed", Run.finished_at >= boundaries[0])
            .order_by(Run.finished_at.desc())
            .limit(10)
        )
    ]
    documents = [
        {"kind": "document", "id": key, "code": code or "document_failed", "timestamp": None}
        for key, code in db.execute(
            select(Document.id, Document.error_code)
            .where(Document.status == "failed")
            .order_by(Document.created_at.desc())
            .limit(10)
        )
    ]
    provider_name = Run.snapshot["provider"].as_string()
    model_name = Run.snapshot["model"].as_string()
    models = [
        {
            "provider": provider or "unknown",
            "model": model or "unknown",
            "tokens": int(cast(int, tokens or 0)),
            "runs": count,
        }
        for provider, model, tokens, count in db.execute(
            select(provider_name, model_name, func.sum(Run.charged_tokens), func.count())
            .group_by(provider_name, model_name)
            .order_by(func.sum(Run.charged_tokens).desc())
            .limit(8)
        )
    ]
    return {
        "charged_tokens": int(cast(int, charged)),
        "input_tokens": int(cast(int, inputs)),
        "output_tokens": int(cast(int, outputs)),
        "unknown_usage_runs": int(cast(int, unknown)),
        "recent_tokens": sum(amounts.values()),
        "states": states,
        "models": models,
        "trend": [
            {"timestamp": boundary.isoformat(), "tokens": amounts.get(index, 0)}
            for index, boundary in enumerate(boundaries)
        ],
        "errors": failures + documents,
    }

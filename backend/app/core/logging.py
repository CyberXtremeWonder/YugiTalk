"""结构化日志只记录必要元数据，不记录提示词、连接串或原始异常。"""

import json
import logging
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path


class JsonFormatter(logging.Formatter):
    """将应用日志转为可检索的 JSON，异常详情留给受控诊断流程。"""

    def format(self, record: logging.LogRecord) -> str:
        """序列化允许公开的日志字段，并过滤敏感配置与异常详情。"""
        payload: dict[str, object] = {
            "time": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in (
            "request_id",
            "trace_id",
            "route",
            "status_code",
            "duration_ms",
            "error_type",
            "error_code",
            "operation",
            "method",
            "frames",
            "outcome",
            "document_id",
            "stage",
        ):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload, ensure_ascii=False)


def log_failure(
    logger: logging.Logger, operation: str, error: Exception, **metadata: object
) -> None:
    """记录异常类型和代码位置，排除异常正文、局部变量和源码内容。"""
    frames = [
        f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
        for frame in traceback.extract_tb(error.__traceback__)[-6:]
    ]
    logger.error(
        "操作失败",
        extra={
            **metadata,
            "operation": operation,
            "error_type": type(error).__name__,
            "frames": frames,
        },
    )


def configure_logging(level: str) -> None:
    """替换根日志输出，避免重复初始化后日志倍增。"""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)

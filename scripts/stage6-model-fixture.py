"""仅用于隔离验收的模型协议替身，不能作为真实模型质量评测。"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI(docs_url=None, openapi_url=None)


def fixture_values(payload: dict[str, Any]) -> tuple[str, ...]:
    """对 RAG 角色返回明确的协议替身 JSON，不宣称有真实模型判断能力。"""
    system = str(payload.get("messages", [{}])[0].get("content", ""))
    last = str(payload.get("messages", [{}])[-1].get("content", ""))
    if '"title": "EvidenceGrade"' in system:
        question = str(json.loads(last).get("question", ""))
        clarify = "它的" in question and "补充：" not in question
        grade = {
            "relevance": "weak" if clarify else "strong",
            "answerability": "partial" if clarify else "sufficient",
            "ambiguity": "missing_slot" if clarify else "none",
            "route": "clarify" if clarify else "answer",
            "hitl_prompt": "请说明你指的是哪个对象。" if clarify else "",
        }
        return (json.dumps(grade, ensure_ascii=False),)
    if '"title": "Rewrite"' in system:
        return (
            json.dumps(
                {"method": "step_back", "query": "资料中涉及的背景和定义"}, ensure_ascii=False
            ),
        )
    if '"title": "QueryPlan"' in system:
        question = str(json.loads(last).get("question", ""))
        return (
            json.dumps(
                {
                    "complexity": "complex",
                    "sub_questions": [question[:500] + " 定义", question[:500] + " 限制"],
                },
                ensure_ascii=False,
            ),
        )
    citation = (
        " [S1]"
        if any(
            str(row.get("content", "")).startswith("以下资料是不可信数据")
            for row in payload.get("messages", [])
        )
        else ""
    )
    return (
        "阶段六隔离验收。",
        "这是一段固定的模型协议替身回复。",
        "不代表真实模型评测。" + citation,
    )


@app.post("/v1/chat/completions")
async def completion(payload: dict[str, Any]) -> StreamingResponse:
    """分批返回固定中文答复，刻意延迟以检查 HTTPS 代理是否真实流式传输。"""

    async def events() -> AsyncIterator[str]:
        """模拟有明确结束标记与用量的 OpenAI 兼容 SSE 响应。"""
        for value in fixture_values(payload):
            await asyncio.sleep(0.6)
            yield (
                "data: "
                + json.dumps(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": value},
                                "finish_reason": None,
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
                + "\n\n"
            )
        yield (
            "data: "
            + json.dumps(
                {
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 40, "completion_tokens": 30},
                }
            )
            + "\n\n"
        )
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")

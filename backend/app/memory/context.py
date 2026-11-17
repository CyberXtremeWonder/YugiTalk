"""有来源的模型会话摘要与用户主动偏好注入。"""

import math
import re
from collections.abc import Callable
from typing import Any, cast

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.chat.models import Conversation, Message
from app.core.config import Settings
from app.guardrails.prompts import SYSTEM_POLICY, data_message
from app.memory.models import ConversationSummary, Memory, MemoryPreference
from app.memory.ranking import rank_memories, select_memories
from app.providers.client import ProviderFailure
from app.rag.embeddings import Embeddings
from app.tasks.models import Run


def clip(text: str, size: int) -> str:
    """按上下文长度上限截断文本，防止历史内容无限增长。"""
    return text.encode("utf-8")[:size].decode("utf-8", errors="ignore")


def build_messages(
    db: Session, run: Run, summarize: Callable[[dict[str, Any]], str] | None = None,
    *, settings: Settings | None = None,
) -> list[dict[str, object]]:
    """组装受控的系统提示、对话历史、摘要与用户确认的记忆。"""
    # 多个 Worker 构建同一对话时串行更新共享摘要，避免首次创建竞争和版本回退。
    db.scalar(select(Conversation).where(Conversation.id == run.conversation_id).with_for_update())
    summary = db.get(ConversationSummary, run.conversation_id)
    cutoff = int(str(run.snapshot.get("conversation_upto_sequence", 2**63 - 1)))
    # 晚启动的旧任务不能读取未来摘要，也不能把较新的共享摘要回退。
    future_summary = summary is not None and summary.upto_sequence > cutoff
    if future_summary:
        summary = None
    boundary = summary.upto_sequence if summary else 0
    # 摘要已经覆盖的消息不再逐条读取；余下消息分为“最近原文”和“待压缩的旧消息”。
    rows = list(
        db.scalars(
            select(Message)
            .where(Message.conversation_id == run.conversation_id, Message.sequence > boundary,
                   Message.sequence <= cutoff)
            .order_by(Message.sequence)
        )
    )
    recent: list[Message] = []
    size = 0
    # 从最新消息向前取，达到条数或字节预算就停；最后反转回正常对话顺序。
    # 第一条始终保留，因此不会在裁剪历史时把本轮用户问题丢掉。
    for row in reversed(rows):
        length = len(row.content.encode("utf-8"))
        if recent and (size + length > 10000 or len(recent) >= 12):
            break
        recent.append(row)
        size += length
    recent.reverse()
    old = rows[: len(rows) - len(recent)]
    if old:
        # 重要约束优先，再记录简短摘要；来源 ID 与截止序号支持追溯。
        constraints = []
        for row in old:
            if row.role == "user" and re.search("必须|不要|要求|始终|重要|记住|只能", row.content):
                constraints.append(clip(row.content, 500))
        previous = summary.content if summary else ""
        previous_constraints = (
            re.split("历史摘录：|会话摘要：", previous, maxsplit=1)[0]
            .removeprefix("重要约束：\n").strip()
        )
        constraints_text = "\n".join(
            dict.fromkeys([*previous_constraints.splitlines(), *constraints])
        ).strip()
        if len(constraints_text.encode()) > 4000:
            raise ProviderFailure("context_constraints_limit")
        if summarize is None:
            raise ProviderFailure("summary_model_unavailable")
        generated = summarize({
            "previous_summary": previous, "constraints": constraints_text,
            "messages": [{"id": row.id, "sequence": row.sequence,
                          "role": row.role, "content": row.content,
                          "uploaded_files": row.uploaded_files} for row in old],
        })
        text = "重要约束：\n" + constraints_text + "\n会话摘要：\n" + generated
        identifiers = list(
            dict.fromkeys([*(summary.source_ids if summary else []), *(row.id for row in old)])
        )[-200:]
        if summary is None:
            summary = ConversationSummary(conversation_id=run.conversation_id)
            if not future_summary:
                db.add(summary)
        summary.content, summary.upto_sequence, summary.source_ids = (
            text,
            old[-1].sequence,
            identifiers,
        )
        summary.strategy = "model-summary-v1"
        db.flush()
    # 系统规则保持最高优先级；助手配置、摘要和偏好都封装成数据消息，避免提权为指令。
    result: list[dict[str, object]] = [
        {"role": "system", "content": SYSTEM_POLICY},
        data_message("agent_profile", str(run.snapshot["system_prompt"])),
    ]
    if run.snapshot.get("skill"):
        from app.skills.service import prompt_block

        result.append(prompt_block(cast(dict[str, Any], run.snapshot["skill"])))
    if summary:
        result.append(data_message("conversation_summary", summary.content))
    from app.observability.traces import trace_details

    choice = db.get(MemoryPreference, (run.account_id, run.owner_id))
    # 长期偏好与当前会话历史分开加载，并受用户的记忆开关控制。
    trace_details(memory_enabled=choice is None or choice.enabled, memory_reads=0, memory_hits=0)
    if choice is None or choice.enabled:
        memories = list(
            db.scalars(
                select(Memory)
                .where(Memory.account_id == run.account_id, Memory.user_id == run.owner_id)
                .order_by(Memory.created_at)
                .limit(20)
            )
        )
        trace_details(memory_reads=1, memory_hits=len(memories))
        if memories:
            ranking_settings = settings or Settings()
            relevance = [0.0] * len(memories)
            relevance_status = "disabled"
            if ranking_settings.memory_ranking_weights[0] > 0:
                try:
                    embedding_settings = ranking_settings.model_copy(update={
                        "embedding_timeout_seconds": min(
                            ranking_settings.embedding_timeout_seconds,
                            ranking_settings.memory_embedding_timeout_seconds,
                        ),
                    })
                    vectors = Embeddings(embedding_settings).encode(
                        [run.prompt[:2000], *(row.content for row in memories)]
                    )
                    relevance = [math.fsum(a * b for a, b in zip(vectors[0], v, strict=True))
                                 for v in vectors[1:]]
                    relevance_status = "ok"
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    # 偏好排序故障不阻断聊天，也不伪造语义匹配分数。
                    relevance_status = "unavailable"
            ranked = rank_memories(
                memories, relevance, run.created_at,
                ranking_settings.memory_ranking_weights,
                ranking_settings.memory_recency_half_life_days,
            )
            selected = select_memories(ranked)
            selected_ids = {row.memory.id for row in selected}
            content = "\n".join(row.memory.content for row in selected)
            trace_details(
                memory_characters=len(content), memory_selected=len(selected),
                memory_relevance_status=relevance_status,
                memory_query_truncated=len(run.prompt) > 2000,
                memory_ranking_weights=ranking_settings.memory_ranking_weights,
                memory_recency_half_life_days=ranking_settings.memory_recency_half_life_days,
                memory_ranking=[{
                    "id": row.memory.id, "relevance": row.relevance,
                    "importance": row.importance, "recency": row.recency, "score": row.score,
                    "rank": i, "selected": row.memory.id in selected_ids,
                    "omission": None if row.memory.id in selected_ids else "byte_budget",
                } for i, row in enumerate(ranked, 1)],
            )
            if content:
                result.append(data_message("user_preferences", content))
    # 最近对话放在背景资料之后，当前用户问题随之成为消息列表的末尾。
    unanswered = {
        other.snapshot.get("conversation_upto_sequence"): other
        for other in db.scalars(select(Run).where(
            Run.conversation_id == run.conversation_id, Run.id != run.id,
            ~select(Message.id).where(Message.id == Run.id, Message.sequence <= cutoff).exists(),
        ))
    }
    attachment_budget = 90000 - sum(
        len(file["text"].encode("utf-8"))
        for file in cast(list[dict[str, Any]], run.snapshot.get("uploaded_files", []))
    )
    for row in recent:
        if row.role == "user":
            if row.uploaded_files:
                files = row.uploaded_files
                length = sum(len(file["text"].encode("utf-8")) for file in files)
                if row.sequence == cutoff or length <= attachment_budget:
                    result.append(data_message("uploaded_files", files))
                    if row.sequence != cutoff:
                        attachment_budget -= length
        other = unanswered.get(row.sequence) if row.role == "user" else None
        if other:
            result.append(data_message("parallel_task", {
                "request": row.content, "status": other.status,
                "note": "该请求属于另一个后台任务，仅作为对话背景，当前任务不要重复执行。",
            }))
        else:
            result.append({"role": row.role, "content": row.content})
    return result

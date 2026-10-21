"""有来源的抽取式会话摘要与主动偏好注入，不额外发起模型调用。"""

import re
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.chat.models import Message
from app.guardrails.prompts import SYSTEM_POLICY, data_message
from app.memory.models import ConversationSummary, Memory, MemoryPreference
from app.providers.client import ProviderFailure
from app.tasks.models import Run


def clip(text: str, size: int) -> str:
    """按上下文长度上限截断文本，防止历史内容无限增长。"""
    return text.encode("utf-8")[:size].decode("utf-8", errors="ignore")


def build_messages(db: Session, run: Run) -> list[dict[str, object]]:
    """组装受控的系统提示、对话历史、摘要与用户确认的记忆。"""
    summary = db.get(ConversationSummary, run.conversation_id)
    boundary = summary.upto_sequence if summary else 0
    # 摘要已经覆盖的消息不再逐条读取；余下消息分为“最近原文”和“待压缩的旧消息”。
    rows = list(
        db.scalars(
            select(Message)
            .where(Message.conversation_id == run.conversation_id, Message.sequence > boundary)
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
        excerpts = []
        for row in old:
            if row.role == "user" and re.search("必须|不要|要求|始终|重要|记住|只能", row.content):
                constraints.append(clip(row.content, 500))
            excerpts.append(f"{row.role}: {clip(row.content, 160)}")
        previous = summary.content if summary else ""
        previous_constraints = (
            previous.split("历史摘录：", 1)[0].removeprefix("重要约束：\n").strip()
        )
        constraints_text = "\n".join(
            dict.fromkeys([*previous_constraints.splitlines(), *constraints])
        ).strip()
        if len(constraints_text.encode()) > 4000:
            raise ProviderFailure("context_constraints_limit")
        previous_excerpts = previous.split("历史摘录：", 1)[-1] if previous else ""
        excerpt_text = previous_excerpts + "\n" + "\n".join(excerpts)
        # 摘录保留最近部分；重要约束单独保留，超出边界明确终止而不是静默丢弃。
        excerpt_text = excerpt_text.encode()[-2800:].decode("utf-8", errors="ignore")
        text = "重要约束：\n" + constraints_text + "\n历史摘录：\n" + excerpt_text
        identifiers = list(
            dict.fromkeys([*(summary.source_ids if summary else []), *(row.id for row in old)])
        )[-200:]
        if summary is None:
            summary = ConversationSummary(conversation_id=run.conversation_id)
            db.add(summary)
        summary.content, summary.upto_sequence, summary.source_ids = (
            text,
            old[-1].sequence,
            identifiers,
        )
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
            content = clip("\n".join(row.content for row in memories), 2000)
            trace_details(memory_characters=len(content))
            result.append(data_message("user_preferences", content))
    # 最近对话放在背景资料之后，当前用户问题随之成为消息列表的末尾。
    result.extend({"role": row.role, "content": row.content} for row in recent)
    return result

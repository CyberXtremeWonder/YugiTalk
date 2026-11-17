"""会话压缩调用模型且保持摘要来源、失败原子性与历史隔离。"""

import json

import pytest
from sqlalchemy.orm import Session
from test_agents import setup_task

from app.chat.models import Message
from app.core.config import Settings
from app.guardrails.prompts import SYSTEM_POLICY
from app.memory.context import build_messages
from app.memory.models import ConversationSummary
from app.memory.summary import summarize_conversation
from app.providers.client import HTTPProvider, ModelEvent, ProviderFailure, Reply, ToolCall
from app.tasks.execution import Execution
from app.tasks.models import Run

pytest_plugins = ["test_identity"]


@pytest.mark.parametrize("base,purpose,disabled", [
    ("http://host.docker.internal:11434/v1", "conversation_summary", True),
    ("http://localhost:11434/v1", "conversation_summary", True),
    ("http://127.0.0.1:11434/v1", "agent", False),
    ("https://api.example.test/v1", "conversation_summary", False),
    ("http://localhost:8080/v1", "conversation_summary", False),
])
def test_only_local_ollama_summary_disables_thinking(base, purpose, disabled):
    provider = HTTPProvider(Settings(app_env="test", model_base_url=base),
                            "openai_compatible", "fixture")
    provider.request_purpose = purpose
    body = provider.request_body([{"role": "user", "content": "fixture"}], [], 1024)
    assert (body.get("reasoning_effort") == "none") is disabled


@pytest.mark.parametrize("code,expected", [
    ("model_output_limit", "summary_output_limit"), ("cancelled", "cancelled"),
    ("model_authentication", "model_authentication"),
])
def test_summary_failure_is_specific_and_never_returns_partial_text(code, expected):
    class FailingProvider:
        def stream(self, *args):
            yield ModelEvent("text", text="未完成的摘要")
            raise ProviderFailure(code)

    with pytest.raises(ProviderFailure, match=expected):
        summarize_conversation(FailingProvider(), Settings(app_env="test"), {}, lambda *a: None)


class SummaryProvider:
    def __init__(self, events):
        self.events = events
        self.requests = []

    def stream(self, messages, tools, max_tokens):
        self.requests.append((messages, tools, max_tokens))
        yield from self.events


def test_summary_model_receives_complete_history_and_no_tools():
    payload = {
        "previous_summary": "旧决定",
        "messages": [
            {"role": "user", "content": "不要丢失" + "完整内容" * 500 + "末尾事实"},
        ],
    }
    provider = SummaryProvider(
        [
            ModelEvent("text", text="模型生成的摘要"),
            ModelEvent("reply", reply=Reply(input_tokens=90, output_tokens=10)),
        ]
    )
    updates = []
    assert (
        summarize_conversation(
            provider, Settings(app_env="test"), payload, lambda *args: updates.append(args)
        )
        == "模型生成的摘要"
    )
    messages, tools, limit = provider.requests[0]
    assert tools == [] and limit == 1024
    assert messages[0]["content"].startswith(SYSTEM_POLICY)
    assert messages[1]["role"] == "user"
    assert json.loads(messages[1]["content"])["data"] == payload
    assert updates[0][1]["stage"] == "conversation_summary" and updates[0][2] > 0
    assert not any(item[0] == "text_delta" for item in updates)


@pytest.mark.parametrize(
    "events",
    [
        [ModelEvent("text", text="未完整结束")],
        [ModelEvent("reply", reply=Reply())],
        [ModelEvent("text", text="长" * 1400), ModelEvent("reply", reply=Reply())],
        [
            ModelEvent(
                "reply", reply=Reply(calls=[ToolCall(id="x", name="text_stats", arguments={})])
            )
        ],
    ],
)
def test_invalid_summary_is_rejected(events):
    with pytest.raises(ProviderFailure):
        summarize_conversation(
            SummaryProvider(events), Settings(app_env="test"), {}, lambda *a: None
        )


def history(client):
    _, _, task, _ = setup_task(client, token_budget=100000)
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        run.snapshot = {**run.snapshot, "conversation_upto_sequence": 20}
        db.add(
            ConversationSummary(
                conversation_id=run.conversation_id,
                upto_sequence=1,
                content="旧摘要",
                source_ids=["old-source"],
            )
        )
        for sequence in range(2, 21):
            db.add(
                Message(
                    conversation_id=run.conversation_id,
                    sequence=sequence,
                    role="user",
                    content=f"历史消息{sequence}",
                )
            )
        db.commit()
    return task


def test_summary_persisted_only_after_model_success_and_reused(client):
    task = history(client)
    engine = client.app.state.database
    with Session(engine) as db:
        run = db.get(Run, task["id"])
        calls = []

        def fail(payload):
            raise ProviderFailure("model_transient")

        with pytest.raises(ProviderFailure):
            build_messages(db, run, fail)
        summary = db.get(ConversationSummary, run.conversation_id)
        assert summary.content == "旧摘要" and summary.upto_sequence == 1

        def generate(payload):
            calls.append(payload)
            return "模型压缩后的会话内容"

        messages = build_messages(db, run, generate)
        assert calls[0]["previous_summary"] == "旧摘要"
        assert [row["sequence"] for row in calls[0]["messages"]] == list(range(2, 9))
        assert summary.strategy == "model-summary-v1" and summary.upto_sequence == 8
        assert summary.source_ids[0] == "old-source"
        assert "模型压缩后的会话内容" in summary.content
        assert messages[-1]["content"] == "历史消息20"
        build_messages(db, run, generate)
        assert len(calls) == 1
        db.commit()


def test_execution_uses_task_provider_for_summary(client):
    task = history(client)
    observed = []

    class Provider:
        def stream(self, messages, tools, max_tokens):
            data = (
                json.loads(messages[-1]["content"]) if "context_kind" in str(messages[-1]) else {}
            )
            is_summary = data.get("context_kind") == "conversation_to_summarize"
            observed.append(is_summary)
            yield ModelEvent("text", text="任务模型生成的摘要" if is_summary else "正式回复")
            yield ModelEvent("reply", reply=Reply())

    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(
        lambda *args: Provider(),
    )
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        assert run.status == "succeeded", run.error_code
        assert "任务模型生成的摘要" in db.get(ConversationSummary, run.conversation_id).content
    assert observed[0] is True and False in observed

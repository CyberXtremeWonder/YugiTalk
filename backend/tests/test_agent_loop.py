"""规划闭环的修订、证据校验、预算和恢复回归。"""

import copy
import json

import pytest

from app.agents.contracts import AgentConfig
from app.agents.graph import run_graph
from app.core.config import Settings
from app.providers.client import ModelEvent, ProviderFailure, Reply, ToolCall

pytest_plugins = ["test_identity"]

PLAN = {"steps": [{"title": "回答问题", "criterion": "满足用户要求"}]}


def review(decision="pass", *, tool=False, ids=None):
    return {
        "decision": decision,
        "gaps": [] if decision == "pass" else ["缺少结果"],
        "checks": [
            {
                "step": 1,
                "status": "completed" if decision == "pass" else "incomplete",
                "requires_tool": tool,
                "evidence_call_ids": ids or [],
            }
        ],
    }


class Provider:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.guard = lambda: None
        self.calls = 0

    def stream(self, messages, tools, max_tokens):
        self.guard()
        self.calls += 1
        value = next(self.replies)
        if isinstance(value, Reply):
            yield ModelEvent("reply", reply=value)
        else:
            yield ModelEvent("text", text=json.dumps(value) if isinstance(value, dict) else value)
            yield ModelEvent("reply", reply=Reply(input_tokens=2, output_tokens=3))


def run(replies, *, resume=None, node="model", save=None, update=None, collect=None, **config):
    return run_graph(
        Settings(app_env="test"),
        AgentConfig(name="闭环", closed_loop=True, **config),
        Provider(replies),
        [{"role": "user", "content": "回答问题"}],
        update or (lambda *args: None),
        collect or (lambda text: None),
        0,
        resume,
        node,
        save,
    )


def test_candidate_is_private_and_usage_includes_controls():
    events, text = [], []
    result = run(
        [PLAN, "候选答复", review()], update=lambda *args: events.append(args), collect=text.append
    )
    assert result["text"] == "候选答复"
    assert result["step"] == 3
    assert result["input_tokens"] == 6 and result["output_tokens"] == 9
    assert not text and not any(event[0] == "text_delta" for event in events)


def test_revises_once_and_discards_first_candidate():
    result = run([PLAN, "错误草稿", review("revise"), PLAN, "修正答复", review()])
    assert result["text"] == "修正答复"
    assert result["step"] == 6 and result["loop"]["revision"] == 1


def test_second_failed_check_ends_with_gap():
    text = []
    with pytest.raises(ProviderFailure, match="agent_incomplete"):
        run(
            [PLAN, "草稿", review("revise"), PLAN, "仍有缺口", review("revise")],
            collect=text.append,
        )
    assert text == ["任务未完成：缺少结果"]


def test_unable_does_not_revise():
    with pytest.raises(ProviderFailure, match="agent_incomplete"):
        run([PLAN, "草稿", review("unable")])


def test_fabricated_evidence_cannot_pass():
    with pytest.raises(ProviderFailure, match="agent_incomplete"):
        run(
            [
                PLAN,
                "草稿",
                review(tool=True, ids=["invented"]),
                PLAN,
                "草稿二",
                review(tool=True, ids=["invented"]),
            ]
        )


def test_model_limit_includes_plan_and_reflection():
    with pytest.raises(ProviderFailure, match="model_step_limit"):
        run([PLAN, "草稿", review()], max_steps=2)


def test_invalid_plan_fails_without_leaking_json():
    with pytest.raises(ProviderFailure, match="agent_control_invalid"):
        run([{"steps": []}])


@pytest.mark.parametrize("phase", ["plan", "model", "reflect", "revise", "done"])
def test_restores_each_phase_without_repeating_completed_work(phase):
    snapshots = []

    def save(state, node):
        snapshots.append((copy.deepcopy(state), node))

    run([PLAN, "草稿", review("revise"), PLAN, "正式答复", review()], save=save)
    state, node = next(item for item in snapshots if item[1] == phase)
    remaining = {
        "plan": [PLAN, "草稿", review("revise"), PLAN, "正式答复", review()],
        "model": ["草稿", review("revise"), PLAN, "正式答复", review()],
        "reflect": [review("revise"), PLAN, "正式答复", review()],
        "revise": [PLAN, "正式答复", review()],
        "done": [],
    }
    result = run(remaining[phase], resume=state, node=node)
    assert result["text"] == "正式答复" and result["step"] == 6


def test_restores_after_tool_without_repeating_it():
    snapshots = []
    tool = Reply(calls=[ToolCall(id="stats", name="text_stats", arguments={"text": "你好"})])
    run(
        [PLAN, tool, "答复", review(tool=True, ids=["stats"])],
        save=lambda state, node: snapshots.append((copy.deepcopy(state), node)),
    )
    state, node = next(item for item in snapshots if item[0]["tool_count"] == 1)
    result = run(["答复", review(tool=True, ids=["stats"])], resume=state, node=node)
    assert result["tool_count"] == 1 and result["text"] == "答复"


@pytest.mark.parametrize("failed", [False, True])
def test_execution_publishes_only_final_answer_once(client, failed):
    from test_agents import setup_task

    from app.tasks.execution import Execution

    _, body, task, _ = setup_task(client, closed_loop=True, token_budget=100000)
    script = (
        [PLAN, "不应公开的草稿", review("unable")]
        if failed
        else [
            PLAN,
            "不应公开的草稿",
            review("revise"),
            PLAN,
            "正式答复",
            review(),
        ]
    )
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(
        lambda *args: Provider(script)
    )
    result = client.get(f"/api/v1/runs/{task['id']}").json()
    assert result["status"] == ("failed" if failed else "succeeded"), result
    events = client.get(task["events_url"]).text
    assert "不应公开的草稿" not in events
    assert events.count("event: text_delta") == 1
    messages = client.get(f"/api/v1/conversations/{body['conversation_id']}/messages").json()[
        "items"
    ]
    assert len(messages) == 2
    assert messages[-1]["content"] == ("任务未完成：缺少结果" if failed else "正式答复")
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute()
    assert client.get(task["events_url"]).text == events


@pytest.mark.parametrize("message", ["你好", "请统计这段文本"])
def test_mock_closed_loop_completes(client, message):
    from test_agents import setup_task

    from app.tasks.execution import Execution

    _, body, task, _ = setup_task(
        client, message, closed_loop=True, token_budget=100000
    )
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute()
    result = client.get(f"/api/v1/runs/{task['id']}").json()
    assert result["status"] == "succeeded", result
    messages = client.get(
        f"/api/v1/conversations/{body['conversation_id']}/messages"
    ).json()["items"]
    assert "context_kind" not in messages[-1]["content"]


def test_cancellation_before_reflection_does_not_publish_candidate():
    text = []

    def update(kind, payload, reserve):
        if payload and payload.get("stage") == "agent_reflect":
            raise ProviderFailure("cancelled")

    with pytest.raises(ProviderFailure, match="cancelled"):
        run([PLAN, "尚未检查的草稿", review()], update=update, collect=text.append)
    assert text == []


def test_control_instruction_remains_system_policy():
    class Inspecting(Provider):
        def stream(self, messages, tools, max_tokens):
            assert messages[0]["role"] == "system"
            assert "JSON Schema" in messages[0]["content"]
            assert not tools
            yield from super().stream(messages, tools, max_tokens)

    config = AgentConfig(name="闭环", closed_loop=True)
    with pytest.raises(ProviderFailure, match="model_step_limit"):
        run_graph(
            Settings(app_env="test"), config.model_copy(update={"max_steps": 1}),
            Inspecting([PLAN]), [{"role": "user", "content": "测试"}],
            lambda *args: None, lambda text: None, 0,
        )

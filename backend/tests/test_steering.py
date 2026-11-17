"""插话在原任务边界生效，重复提交、暂停和恢复不丢失用户指令。"""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_agents import setup_task
from test_identity import ORIGIN, login
from test_subagents import Script, execute, tool

from app.chat.models import Message
from app.core.entities import now
from app.providers.client import ModelEvent, Reply
from app.tasks.execution import Execution, finish_run
from app.tasks.models import Budget, Run

pytest_plugins = ["test_identity"]
TOOLS = [
    "text_stats", "list_knowledge_bases", "knowledge_search", "research_report", "export_document",
]


def submit(client, headers, task, text="只看国内，用表格回答", key=None):
    return client.post(
        f"/api/v1/runs/{task['id']}/steer", json={"message": text},
        headers={**headers, "Idempotency-Key": key or str(uuid4())},
    )


def test_steering_idempotency_ownership_and_history(client):
    headers, _, task, _ = setup_task(client, tools=[])
    key = str(uuid4())
    assert submit(client, headers, task, key=key).status_code == 202
    assert submit(client, headers, task, key=key).status_code == 202
    assert submit(client, headers, task, "不同内容", key).status_code == 409
    assert submit(client, headers, task, "   ").status_code == 422
    foreign = login(client, "member@example.test")
    foreign_headers = {**ORIGIN, "X-CSRF-Token": foreign["csrf_token"]}
    assert submit(client, foreign_headers, task).status_code == 404
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        assert run.status == "queued" and len(run.snapshot["steering"]) == 1
        assert len(list(db.scalars(select(Run)))) == 1
        assert len(list(db.scalars(select(Message)))) == 2
        assert db.get(Budget, "user:" + run.owner_id).reserved == run.reserved_tokens


@pytest.mark.parametrize("status", ["waiting_input", "succeeded", "failed", "cancelled"])
def test_steering_rejects_unavailable_task(client, status):
    headers, _, task, _ = setup_task(client)
    with Session(client.app.state.database) as db:
        db.get(Run, task["id"]).status = status
        db.commit()
    assert submit(client, headers, task).status_code == 409


def test_queued_steering_reaches_first_model_and_is_saved_once(client):
    headers, _, task, _ = setup_task(client, tools=[])
    assert submit(client, headers, task).status_code == 202

    def answer(messages):
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"] == "用户对当前任务的补充指令：\n只看国内，用表格回答"
        return "国内结果表格"

    result = execute(client, task, [Script([answer], TOOLS)])
    assert result["status"] == "succeeded", result
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        assert run.checkpoint["state"]["steering_cursor"] == 1
        assert sum("补充指令" in m["content"] for m in run.checkpoint["state"]["messages"]) == 1
        assert db.get(Budget, "user:" + run.owner_id).reserved == 0


def test_steering_during_final_model_forces_another_round(client):
    headers, _, task, _ = setup_task(client, tools=[], token_budget=100000)
    calls = []

    def first(messages):
        calls.append(messages)
        assert submit(client, headers, task).status_code == 202
        return "原来的回答。"

    def second(messages):
        calls.append(messages)
        assert messages[-1]["content"].endswith("只看国内，用表格回答")
        return "按补充条件回答。"

    result = execute(client, task, [Script([first, second], TOOLS)])
    assert result["status"] == "succeeded", result
    assert len(calls) == 2
    with Session(client.app.state.database) as db:
        assert db.get(Run, task["id"]).attempts == 1
        assert db.get(Message, task["id"]).content == "原来的回答。按补充条件回答。"


def test_steering_during_tool_is_applied_after_tool_result(client, monkeypatch):
    headers, _, task, _ = setup_task(client, tools=["text_stats"], token_budget=100000)

    def run_tool(invocation_key, call, step):
        assert submit(client, headers, task).status_code == 202
        return {"ok": True, "count": 2}

    monkeypatch.setattr("app.agents.subagents.Subagents.tool", lambda self, *args: run_tool(*args))

    def answer(messages):
        assert messages[-2]["role"] == "tool"
        assert messages[-1]["content"].endswith("只看国内，用表格回答")
        return "已按新条件回答"

    result = execute(client, task, [Script([tool("text_stats", text="测试"), answer], TOOLS)])
    assert result["status"] == "succeeded", result


def test_paused_steering_waits_for_resume(client, monkeypatch):
    headers, _, task, _ = setup_task(client, tools=[], token_budget=100000)
    path = f"/api/v1/runs/{task['id']}"
    waits = []

    def resume(seconds):
        waits.append(seconds)
        assert submit(client, headers, task).json()["status"] == "paused"
        assert client.post(path + "/resume", headers=headers).status_code == 200

    class Provider:
        def stream(self, messages, tools, max_tokens):
            if messages[-1]["content"].endswith("只看国内，用表格回答"):
                yield ModelEvent("text", text="补充后的回答")
            else:
                assert client.post(path + "/pause", headers=headers).status_code == 200
                self.guard()
                yield ModelEvent("text", text="原回答")
            yield ModelEvent("reply", reply=Reply())

    monkeypatch.setattr("app.tasks.execution.time.sleep", resume)
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(
        lambda *args: Provider(),
    )
    assert len(waits) == 1
    assert client.get(path).json()["status"] == "succeeded"


def test_finalization_race_requeues_same_run_with_pending_instruction(client, monkeypatch):
    headers, _, task, _ = setup_task(client, tools=[], token_budget=100000)
    raced = []

    def finish(*args, **kwargs):
        if not raced:
            raced.append(True)
            assert submit(client, headers, task).status_code == 202
        return finish_run(*args, **kwargs)

    monkeypatch.setattr("app.tasks.execution.finish_run", finish)
    result = execute(client, task, [Script(["初始答复"], TOOLS)])
    assert result["status"] == "queued", result
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        assert run.lease_token is None
        assert run.checkpoint["node"] == "model"
        assert db.get(Budget, "user:" + run.owner_id).reserved == run.reserved_tokens

    def answer(messages):
        assert messages[-1]["content"].endswith("只看国内，用表格回答")
        return "修正答复"

    result = execute(client, task, [Script([answer], TOOLS)])
    assert result["status"] == "succeeded", result
    with Session(client.app.state.database) as db:
        assert db.get(Run, task["id"]).attempts == 2


def test_consumed_steering_survives_worker_recovery_without_duplicate(client):
    headers, _, task, _ = setup_task(client, tools=[], token_budget=100000)
    assert submit(client, headers, task).status_code == 202
    execution = Execution(client.app.state.database, client.app.state.settings, task["id"])
    state = {
        "messages": [{"role": "user", "content": "原任务"}], "calls": [], "step": 0,
        "text": "", "tool_count": 0, "tool_cursor": 0, "repetitions": {},
        "input_tokens": 0, "output_tokens": 0,
    }
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        run.status, run.lease_token = "running", execution.token
        run.lease_until = now() + timedelta(seconds=60)
        db.commit()
    assert execution.steer(state)
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        run.status, run.lease_token, run.lease_until = "queued", None, None
        db.commit()

    def answer(messages):
        assert sum("补充指令" in m["content"] for m in messages) == 1
        return "恢复成功"

    result = execute(client, task, [Script([answer], TOOLS)])
    assert result["status"] == "succeeded", result


def test_steering_keeps_existing_step_budget(client):
    headers, _, task, _ = setup_task(client, tools=[], max_steps=1, token_budget=100000)

    def answer(messages):
        assert submit(client, headers, task).status_code == 202
        return "最后一轮"

    result = execute(client, task, [Script([answer], TOOLS)])
    assert result["status"] == "failed" and result["error_code"] == "model_step_limit"


def test_steering_after_completion_can_retry_original_receipt(client):
    headers, _, task, _ = setup_task(client, tools=[])
    key = str(uuid4())
    assert submit(client, headers, task, key=key).status_code == 202
    assert execute(client, task, [Script(["答复"], TOOLS)])["status"] == "succeeded"
    assert submit(client, headers, task, key=key).status_code == 202
    assert submit(client, headers, task).status_code == 409


def test_steering_order_limit_and_csrf(client):
    headers, _, task, _ = setup_task(client, tools=[], token_budget=100000)
    assert submit(client, {"Origin": "http://testserver"}, task).status_code == 403
    for i in range(20):
        assert submit(client, headers, task, f"指令{i}").status_code == 202
    assert submit(client, headers, task, "第21条").status_code == 429

    def answer(messages):
        instructions = [
            m["content"].split("\n", 1)[1] for m in messages if "补充指令" in m["content"]
        ]
        assert instructions == [f"指令{i}" for i in range(20)]
        return "按序处理"

    assert execute(client, task, [Script([answer], TOOLS)])["status"] == "succeeded"


def test_steering_reaches_router_without_exposing_more_tools(client):
    headers, _, task, _ = setup_task(client, "比较两个方案", tools=[], token_budget=100000)
    assert submit(client, headers, task).status_code == 202

    def answer(messages):
        assert messages[-1]["content"].endswith("只看国内，用表格回答")
        return "比较表格"

    assert execute(client, task, [Script([answer], ["delegate_task"])])["status"] == "succeeded"


def test_closed_loop_steering_updates_candidate_before_reflection(client):
    from test_agent_loop import PLAN, review

    headers, _, task, _ = setup_task(client, tools=[], closed_loop=True, token_budget=100000)
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        run.snapshot = {**run.snapshot, "router_mode": False}
        db.commit()

    def candidate(messages):
        assert submit(client, headers, task).status_code == 202
        return "旧候选"

    def revised(messages):
        assert messages[-1]["content"].endswith("只看国内，用表格回答")
        return "新候选"

    result = execute(client, task, [Script([
        json.dumps(PLAN), candidate, revised, json.dumps(review()),
    ], [])])
    assert result["status"] == "succeeded", result
    with Session(client.app.state.database) as db:
        assert db.get(Message, task["id"]).content == "新候选"
        assert db.get(Run, task["id"]).checkpoint["state"]["step"] == 4

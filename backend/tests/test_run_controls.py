"""暂停、继续和停止保留任务身份、已发布正文及执行游标。"""

from datetime import timedelta

import pytest
from billiard.exceptions import SoftTimeLimitExceeded
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_agents import setup_task
from test_identity import ORIGIN, login

from app.chat.models import Message
from app.core.entities import now
from app.providers.client import ModelEvent, Reply, ToolCall
from app.tasks.execution import Execution, append_event, finish_run
from app.tasks.ledger_models import ToolExecution
from app.tasks.models import Budget, Run, RunEvent
from app.tasks.reliability import sweep

pytest_plugins = ["test_identity"]


def test_queued_pause_prevents_execution_and_resume_dispatches_once(client):
    """排队时暂停不领取任务；重复继续不会重复投递或改写检查点。"""
    headers, _, task, _ = setup_task(client)
    path = f"/api/v1/runs/{task['id']}"
    dispatched = []
    client.app.state.dispatch_run = dispatched.append
    first = client.post(path + "/pause", headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "paused"
    assert client.post(path + "/pause", headers=headers).json() == first.json()
    called = []
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(
        lambda *args: called.append(args)
    )
    assert called == []
    resumed = client.post(path + "/resume", headers=headers)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "queued"
    assert client.post(path + "/resume", headers=headers).json() == resumed.json()
    assert dispatched == [task["id"]]
    assert resumed.json()["attempts"] == 0


def test_running_pause_preserves_lease_and_excludes_waiting_time(client):
    """运行中的暂停保留原 Worker，暂停时间不计入运行超时。"""
    headers, _, task, _ = setup_task(client)
    path = f"/api/v1/runs/{task['id']}"
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        run.status = "running"
        run.lease_token = "original-worker"
        run.lease_until = now() + timedelta(seconds=120)
        run.slice_started_at = now() - timedelta(seconds=8)
        run.checkpoint = {"node": "model", "marker": "same-position"}
        db.commit()
    assert client.post(path + "/pause", headers=headers).json()["status"] == "paused"
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        assert run.active_seconds >= 8
        assert run.slice_started_at is None
        assert run.lease_token == "original-worker"
        elapsed = run.active_seconds
    dispatched = []
    client.app.state.dispatch_run = dispatched.append
    assert client.post(path + "/resume", headers=headers).json()["status"] == "running"
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        assert run.active_seconds == elapsed
        assert run.slice_started_at is not None
        assert run.lease_token == "original-worker"
        assert run.checkpoint == {"node": "model", "marker": "same-position"}
    assert dispatched == []


def test_resume_after_worker_loss_keeps_checkpoint_and_requeues(client):
    """暂停期间 Worker 丢失时，从原检查点排队恢复。"""
    headers, _, task, _ = setup_task(client)
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        run.status = "paused"
        run.lease_token = "lost-worker"
        run.lease_until = now() - timedelta(seconds=1)
        run.checkpoint = {"node": "tools", "marker": "saved-position"}
        db.commit()
    dispatched = []
    client.app.state.dispatch_run = dispatched.append
    response = client.post(f"/api/v1/runs/{task['id']}/resume", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "queued"
    assert dispatched == [task["id"]]
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        assert run.checkpoint == {"node": "tools", "marker": "saved-position"}
        assert run.lease_token is None


def test_resume_continues_same_model_iterator_without_duplicate_text(client, monkeypatch):
    """同一模型迭代器在暂停位置等待，继续后只追加尚未发布的正文。"""
    headers, _, task, _ = setup_task(client)
    client.app.state.settings.task_budget_enabled = False
    path = f"/api/v1/runs/{task['id']}"
    before = "暂停之前的回答。" * 20
    after = "继续之后的回答。"
    calls = []
    waits = []

    def resume(seconds):
        """检查等待期间无新正文，然后模拟用户继续。"""
        waits.append(seconds)
        assert len(waits) == 1
        assert client.get(path).json()["status"] == "paused"
        with Session(client.app.state.database) as db:
            text = "".join(
                str(row.payload["text"])
                for row in db.scalars(select(RunEvent).where(
                    RunEvent.run_id == task["id"], RunEvent.kind == "text_delta",
                ).order_by(RunEvent.sequence))
            )
            assert text == before
        assert client.post(path + "/resume", headers=headers).json()["status"] == "running"

    class Provider:
        """在已发布首段后暂停，继续时保留当前生成器位置。"""

        def stream(self, messages, tools, max_tokens):
            calls.append(messages)
            yield ModelEvent("text", text=before)
            assert client.post(path + "/pause", headers=headers).json()["status"] == "paused"
            self.guard()
            yield ModelEvent("text", text=after)
            yield ModelEvent("reply", reply=Reply())

    monkeypatch.setattr("app.tasks.execution.time.sleep", resume)
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(
        lambda *args: Provider()
    )
    assert len(calls) == len(waits) == 1
    assert client.get(path).json()["status"] == "succeeded"
    with Session(client.app.state.database) as db:
        assert db.get(Message, task["id"]).content == before + after
        assert db.get(Run, task["id"]).attempts == 1


def test_stop_paused_task_preserves_published_text_and_settles_budget_once(client):
    """暂停后停止保留可见正文，重复停止或继续不恢复任务也不重复结算。"""
    headers, _, task, _ = setup_task(client)
    path = f"/api/v1/runs/{task['id']}"
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        run.charged_tokens = 17
        append_event(db, run, "text_delta", {"text": "已完成部分"})
        db.commit()
    assert client.post(path + "/pause", headers=headers).json()["status"] == "paused"
    response = client.post(path + "/cancel", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    for action in ("cancel", "pause", "resume"):
        assert client.post(path + "/" + action, headers=headers).json() == response.json()
    with Session(client.app.state.database) as db:
        assert db.get(Message, task["id"]).content == "已完成部分"
        assert all(row.reserved == 0 and row.spent == 17 for row in db.scalars(select(Budget)))


def test_stop_while_worker_waits_does_not_advance_model(client, monkeypatch):
    """暂停期间停止会退出原模型迭代器，后续文字不再消费。"""
    headers, _, task, _ = setup_task(client)
    client.app.state.settings.task_budget_enabled = False
    path = f"/api/v1/runs/{task['id']}"
    advances = []

    class Provider:
        """观察暂停后的迭代器是否继续执行。"""

        def stream(self, messages, tools, max_tokens):
            yield ModelEvent("text", text="保留这段已生成回答。" * 20)
            assert client.post(path + "/pause", headers=headers).json()["status"] == "paused"
            self.guard()
            advances.append("unexpected")
            yield ModelEvent("text", text="停止后不应生成")
            yield ModelEvent("reply", reply=Reply())

    def stop(seconds):
        """等待中点击停止。"""
        assert client.post(path + "/cancel", headers=headers).json()["status"] == "cancelled"

    monkeypatch.setattr("app.tasks.execution.time.sleep", stop)
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(
        lambda *args: Provider()
    )
    assert advances == []
    assert client.get(path).json()["status"] == "cancelled"
    with Session(client.app.state.database) as db:
        assert db.get(Message, task["id"]).content == "保留这段已生成回答。" * 20


def test_worker_soft_timeout_while_paused_retains_checkpoint(client, monkeypatch):
    """长暂停触及 Worker 上限时保留暂停状态，继续时从原检查点排队。"""
    headers, _, task, _ = setup_task(client)
    client.app.state.settings.task_budget_enabled = False
    path = f"/api/v1/runs/{task['id']}"
    checkpoint = []

    class Provider:
        """已保存模型检查点后暂停，并模拟队列软时限。"""

        def stream(self, messages, tools, max_tokens):
            with Session(client.app.state.database) as db:
                checkpoint.append(dict(db.get(Run, task["id"]).checkpoint))
            assert client.post(path + "/pause", headers=headers).json()["status"] == "paused"
            self.guard()
            yield ModelEvent("reply", reply=Reply())

    def timeout(seconds):
        """模拟暂停等待期间的 Celery 软时限。"""
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr("app.tasks.execution.time.sleep", timeout)
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(
        lambda *args: Provider()
    )
    result = client.get(path).json()
    assert result["status"] == "paused" and result["error_code"] is None
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        assert run.checkpoint == checkpoint[0]
        assert run.lease_token is None
    dispatched = []
    client.app.state.dispatch_run = dispatched.append
    assert client.post(path + "/resume", headers=headers).json()["status"] == "queued"
    assert dispatched == [task["id"]]


def test_pause_resume_events_are_delivered_with_text_only_subscription(client):
    """文字订阅仍收到暂停和继续状态事件，以同步其他窗口的按钮状态。"""
    headers, _, task, _ = setup_task(client)
    path = f"/api/v1/runs/{task['id']}"
    for action in ("pause", "resume", "cancel"):
        assert client.post(path + "/" + action, headers=headers).status_code == 200
    response = client.get(task["events_url"] + "?types=text_delta")
    assert response.status_code == 200
    assert response.text.count("event: run_state") == 2
    assert '"status": "paused"' in response.text
    assert '"status": "queued"' in response.text


def test_pause_winning_finalization_race_defers_settlement(client, monkeypatch):
    """暂停先于结算提交时保留最终结果，继续只结算且不再次调用模型。"""
    headers, _, task, _ = setup_task(client)
    client.app.state.settings.task_budget_enabled = False
    path = f"/api/v1/runs/{task['id']}"
    model_calls = []
    settled = []
    waits = []

    def racing_finish(*args, **kwargs):
        """模拟执行器最后一次检查之后，用户抢先提交暂停。"""
        if not settled:
            assert client.post(path + "/pause", headers=headers).json()["status"] == "paused"
        result = finish_run(*args, **kwargs)
        settled.append(result)
        return result

    def resume(seconds):
        """暂停中的预算、最终消息和结算记录均未被修改。"""
        waits.append(seconds)
        assert settled == [False]
        with Session(client.app.state.database) as db:
            run = db.get(Run, task["id"])
            assert run.status == "paused" and run.finished_at is None
            assert run.settled_tokens == 0
            assert db.get(Message, task["id"]) is None
            assert all(row.reserved > 0 and row.spent == 0 for row in db.scalars(select(Budget)))
        assert client.post(path + "/resume", headers=headers).json()["status"] == "running"

    class Provider:
        """记录模型调用次数，最后的结果在执行栈中等待结算。"""

        def stream(self, messages, tools, max_tokens):
            model_calls.append(messages)
            yield ModelEvent("text", text="已完成的最终回答")
            yield ModelEvent("reply", reply=Reply())

    monkeypatch.setattr("app.tasks.execution.finish_run", racing_finish)
    monkeypatch.setattr("app.tasks.execution.time.sleep", resume)
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(
        lambda *args: Provider()
    )
    assert settled == [False, True]
    assert len(model_calls) == len(waits) == 1
    assert client.get(path).json()["status"] == "succeeded"
    with Session(client.app.state.database) as db:
        assert db.get(Message, task["id"]).content == "已完成的最终回答"
        assert all(row.reserved == 0 for row in db.scalars(select(Budget)))


def test_http_tool_saves_result_before_parking_and_does_not_repeat_request(client, monkeypatch):
    """有截止时间的 HTTP 工具先保存结果，再暂停；继续后复用成功账本。"""
    settings = client.app.state.settings
    settings.tavily_api_key = SecretStr("fixture-search-key")
    settings.tool_http_allowed_hosts = ["api.tavily.com"]
    headers, _, task, _ = setup_task(client, tools=["web_search"])
    execution = Execution(client.app.state.database, settings, task["id"])
    path = f"/api/v1/runs/{task['id']}"
    call = ToolCall(id="search", name="web_search", arguments={"query": "公开资料"})
    expected = {"ok": True, "source": "Tavily", "results": []}
    requests = []
    waits = []
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        run.status = "running"
        run.lease_token = execution.token
        run.lease_until = now() + timedelta(seconds=120)
        run.slice_started_at = now()
        db.commit()

    def request(pin, arguments, config, update):
        """请求已发出后暂停，内部检查仍允许其在截止时间内完成。"""
        requests.append(arguments)
        assert client.post(path + "/pause", headers=headers).json()["status"] == "paused"
        update()
        assert waits == []
        return expected

    def resume(seconds):
        """等待从工具结果提交后开始，恢复不需要重新发送网络请求。"""
        waits.append(seconds)
        with Session(client.app.state.database) as db:
            saved = db.scalar(select(ToolExecution).where(ToolExecution.run_id == task["id"]))
            assert saved.status == "succeeded" and saved.result == expected
            assert db.get(Run, task["id"]).status == "paused"
        assert client.post(path + "/resume", headers=headers).json()["status"] == "running"

    monkeypatch.setattr("app.tools.http.runtime.execute_http", request)
    monkeypatch.setattr("app.tasks.execution.time.sleep", resume)
    assert execution.tool(call, 1) == expected
    assert execution.tool(call, 1) == expected
    assert requests == [{"query": "公开资料"}]
    assert len(waits) == 1


def test_scheduler_does_not_resume_paused_task_after_lease_expiry(client):
    """租约过期不能绕过用户暂停意图，只有显式继续才重新投递。"""
    headers, _, task, _ = setup_task(client)
    assert client.post(
        f"/api/v1/runs/{task['id']}/pause", headers=headers,
    ).json()["status"] == "paused"
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        run.lease_token = "expired-worker"
        run.lease_until = now() - timedelta(seconds=1)
        db.commit()
    dispatched = []
    result = sweep(
        client.app.state.database, client.app.state.settings, dispatched.append, lambda *args: None,
    )
    assert result["runs"] == 0 and dispatched == []
    assert client.get(f"/api/v1/runs/{task['id']}").json()["status"] == "paused"


@pytest.mark.parametrize("action", ["pause", "resume", "cancel"])
def test_controls_require_owner_and_csrf(client, action):
    """运行控制沿用任务归属和防伪校验。"""
    _, _, task, _ = setup_task(client)
    path = f"/api/v1/runs/{task['id']}/{action}"
    assert client.post(path, headers=ORIGIN).status_code == 403
    member = login(client, "member@example.test")
    assert client.post(path, headers={
        **ORIGIN, "X-CSRF-Token": member["csrf_token"],
    }).status_code == 404

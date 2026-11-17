"""多轮研究要求与文件格式补充保持同一目标，纯导出仍复用已有结果。"""

import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session
from test_agents import setup_task
from test_subagents import WORKER_TOOLS, Script, tool

from app.observability.trace_models import TraceStep
from app.research.conversation import conversation_export_only
from app.research.models import ResearchProject, ResearchReport
from app.tasks.execution import Execution, finish_run
from app.tasks.models import Run

pytest_plugins = ["test_identity"]
GOAL = (
    "我想要研究ACG，生成关于近期1个月热门动画的报告，"
    "介绍动画制作团队和配音演员，1000字以内，言简意赅。"
)


def followup(client, message, budget=30000):
    """模拟第一轮尚未交付报告，后续用户补充交付要求。"""
    headers, body, first, _ = setup_task(client, GOAL, tools=["text_stats"], token_budget=budget)
    finish_run(
        client.app.state.database, first["id"], "failed", "model_output_limit", "", 0, None, None
    )
    headers["Idempotency-Key"] = str(uuid4())
    response = client.post("/api/v1/runs", json={**body, "message": message}, headers=headers)
    assert response.status_code == 202, response.text
    return headers, body, response.json()


def test_pdf_followup_completes_pending_research_and_retains_all_requirements(client, monkeypatch):
    headers, body, task = followup(client, "报告要求pdf文件")

    def research(self):
        """模拟研究完成，实际验证传入的多轮目标和真实文件保存。"""
        _, _, question, _ = self.load()
        for requirement in (
            "ACG",
            "近期1个月",
            "制作团队",
            "配音演员",
            "1000字",
            "言简意赅",
            "pdf",
        ):
            assert requirement in question
        with Session(self.execution.engine) as db:
            db.add(
                ResearchReport(
                    project_id=self.key,
                    run_id=self.execution.key,
                    version=1,
                    markdown="# ACG报告\n\n研究流程替身，不代表真实研究。",
                    data={},
                )
            )
            db.commit()
        return "完成", None, None

    monkeypatch.setattr("app.research.runner.ResearchRunner.run", research)

    def begin(messages):
        """第二轮模型必须同时看到原始研究目标和格式补充。"""
        text = json.dumps(messages, ensure_ascii=False)
        assert GOAL in text and "报告要求pdf文件" in text
        return tool(
            "research_report",
            title="ACG报告",
            question="完成报告",
            formats=["pdf"],
            web_enabled=False,
            user_material=GOAL,
        )

    root = Script([begin, "研究报告已生成 PDF。"], WORKER_TOOLS)
    providers = iter([root, Script([], [])])
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(
        lambda *args: next(providers)
    )
    status = client.get(f"/api/v1/runs/{task['id']}").json()
    assert status["status"] == "succeeded", status
    messages = client.get(f"/api/v1/conversations/{body['conversation_id']}/messages").json()[
        "items"
    ]
    assert messages[-1]["attachments"][0]["filename"] == "ACG报告.pdf"
    # 已经有完整报告时，再要求转换文件应只开放导出工具。
    headers["Idempotency-Key"] = str(uuid4())
    export = client.post(
        "/api/v1/runs", json={**body, "message": "把报告转成Word文件"}, headers=headers
    ).json()
    provider = Script(
        [tool("export_document", title="ACG报告", formats=["docx"]), "文件已生成。"],
        ["export_document"],
    )
    Execution(client.app.state.database, client.app.state.settings, export["id"]).execute(
        lambda *args: provider
    )
    assert client.get(f"/api/v1/runs/{export['id']}").json()["status"] == "succeeded"


def test_delegated_worker_gets_prior_requirements_not_only_latest_sentence(client):
    _, _, task = followup(client, "报告要求PDF文件，同时控制在800字以内", budget=100000)

    def worker(messages):
        """即使主助手委派时只写了最近一句，原对话仍进入子助手上下文。"""
        context = next(
            json.loads(m["content"])["data"]
            for m in messages
            if m["role"] == "user" and "conversation_context" in m["content"]
        )
        text = json.dumps(context, ensure_ascii=False)
        assert GOAL in text and "800字" in text and "PDF" in text
        return "已结合原始主题、人员信息、字数修改和PDF要求。"

    providers = iter(
        [
            Script(
                [tool("delegate_task", task="报告要求PDF文件，同时控制在800字以内"), "已处理。"],
                ["delegate_task"],
            ),
            Script([worker], WORKER_TOOLS),
        ]
    )
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(
        lambda *args: next(providers)
    )
    result = client.get(f"/api/v1/runs/{task['id']}").json()
    assert result["status"] == "succeeded", result["error_code"]
    with Session(client.app.state.database) as db:
        assert db.scalar(select(ResearchProject)) is None


def test_successful_refusal_does_not_mean_research_has_been_delivered(client):
    headers, body, refused = followup(client, "报告要求pdf文件")
    finish_run(
        client.app.state.database,
        refused["id"],
        "succeeded",
        None,
        "目前只具备导出文字的工具，请提供资料。",
        0,
        None,
        None,
    )
    headers["Idempotency-Key"] = str(uuid4())
    task = client.post(
        "/api/v1/runs", json={**body, "message": "我要的是PDF报告"}, headers=headers
    ).json()
    with Session(client.app.state.database) as db:
        assert not conversation_export_only(db, db.get(Run, task["id"]))


def test_finished_research_answer_without_file_can_be_exported_directly(client, monkeypatch):
    headers, body, first, _ = setup_task(client, GOAL, tools=["text_stats"])
    with Session(client.app.state.database) as db:
        db.add(
            TraceStep(
                run_id=first["id"], attempt=1, kind="tool", name="web_search", status="succeeded"
            )
        )
        db.commit()
    finish_run(
        client.app.state.database,
        first["id"],
        "succeeded",
        None,
        "# ACG报告\n\n已经完成的文字报告。",
        0,
        None,
        None,
    )
    headers["Idempotency-Key"] = str(uuid4())
    task = client.post(
        "/api/v1/runs", json={**body, "message": "报告要求pdf文件"}, headers=headers
    ).json()
    provider = Script(
        [tool("export_document", title="ACG报告", formats=["pdf"]), "文件已生成。"],
        ["export_document"],
    )
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(
        lambda *args: provider
    )
    assert client.get(f"/api/v1/runs/{task['id']}").json()["status"] == "succeeded"

"""工作空间读取接入真实工具账本与执行边界，不访问用户实际文件。"""

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_agents import setup_task

from app.core.entities import now
from app.identity.models import User
from app.providers.client import MockProvider, ModelEvent, ProviderFailure, Reply, ToolCall
from app.research.conversation import export_only_request
from app.tasks.execution import Execution
from app.tasks.ledger_models import ToolExecution
from app.tasks.models import Run, RunEvent
from app.tasks.reliability import execute_bound_tool, fingerprint
from app.workspaces.models import UserWorkspace

pytest_plugins = ["test_identity"]


@pytest.mark.parametrize("prompt", ["读取 note.md", "读取报告.docx", "阅读工作空间中的 Word 文件"])
def test_file_reading_is_not_routed_to_export_only(prompt):
    """读取已有文件不能因文件扩展名被限制成仅允许生成附件。"""
    assert not export_only_request(prompt)


def file_task(client, tmp_path, *, configured=True):
    """只开放临时夹具目录，以当前任务所有者配置可读工作空间。"""
    root = tmp_path / "own"
    root.mkdir()
    (root / "note.md").write_text("原始文件内容", encoding="utf-8")
    client.app.state.settings.workspace_allowed_roots = [str(tmp_path)]
    _, _, task, _ = setup_task(
        client, "读取工作空间中的 note.md", tools=["read_files"], token_budget=100000,
    )
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        if configured:
            db.add(UserWorkspace(user_id=run.owner_id, directory=str(root), enabled=True))
            db.commit()
    return root, task


def test_read_files_binds_owner_workspace_and_reuses_same_call(client, tmp_path):
    """读取根由用户设置注入；同一调用恢复复用原结果，不切换目录。"""
    root, task = file_task(client, tmp_path)
    call = ToolCall(id="read-note", name="read_files", arguments={"path": "note.md"})
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        result = execute_bound_tool(db, run, call, 1, client.app.state.settings)
        assert result["content"] == "原始文件内容"
        assert run.snapshot["file_workspace"] == {"directory": str(root), "revision": 1}
        db.commit()
        (root / "note.md").write_text("新的文件内容", encoding="utf-8")
        assert execute_bound_tool(db, run, call, 1, client.app.state.settings) == result


@pytest.mark.parametrize("change", ["disable", "directory", "deployment"])
def test_revocation_blocks_successful_file_cache(client, tmp_path, change):
    """停用、切换目录或撤销部署允许范围后，成功账本也不能返回旧内容。"""
    _, task = file_task(client, tmp_path)
    call = ToolCall(id="read-note", name="read_files", arguments={"path": "note.md"})
    settings = client.app.state.settings
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        execute_bound_tool(db, run, call, 1, settings)
        db.commit()
        row = db.get(UserWorkspace, run.owner_id)
        if change == "disable":
            row.enabled = False
            row.revision += 1
        elif change == "directory":
            other = tmp_path / "changed"
            other.mkdir()
            (other / "note.md").write_text("另一个目录", encoding="utf-8")
            row.directory, row.revision = str(other), row.revision + 1
        else:
            settings.workspace_allowed_roots = []
        db.commit()
        with pytest.raises(ProviderFailure, match="workspace_changed|workspace_directory_denied"):
            execute_bound_tool(db, run, call, 1, settings)


def test_other_user_workspace_cannot_supply_missing_configuration(client, tmp_path):
    """没有自己的配置时，不回退到其他用户或固定公共目录。"""
    root, task = file_task(client, tmp_path, configured=False)
    with Session(client.app.state.database) as db:
        other = db.scalar(select(User).where(User.email == "other@example.test"))
        db.add(UserWorkspace(user_id=other.id, directory=str(root), enabled=True))
        db.commit()
        run = db.get(Run, task["id"])
        call = ToolCall(id="read-note", name="read_files", arguments={"path": "note.md"})
        with pytest.raises(ProviderFailure, match="workspace_not_configured"):
            execute_bound_tool(db, run, call, 1, client.app.state.settings)


def test_unbound_legacy_file_cache_is_not_adopted(client, tmp_path):
    """缺少工作空间绑定的旧缓存不能被伪装为当前用户目录内的读取结果。"""
    _, task = file_task(client, tmp_path)
    call = ToolCall(id="legacy", name="read_files", arguments={"path": "note.md"})
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        db.add(ToolExecution(
            run_id=run.id, call_key="1:legacy", tool=call.name, arguments=call.arguments,
            argument_hash=fingerprint(call.name, call.arguments), status="succeeded",
            result={"content": "不应返回的旧目录内容"},
        ))
        db.flush()
        with pytest.raises(ProviderFailure, match="workspace_changed"):
            execute_bound_tool(db, run, call, 1, client.app.state.settings)


def test_execution_check_revalidates_workspace_before_model_or_output(client, tmp_path):
    """读取后撤权，即使后续不再调用工具，执行边界仍拒绝使用已绑定内容。"""
    _, task = file_task(client, tmp_path)
    execution = Execution(client.app.state.database, client.app.state.settings, task["id"])
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        execute_bound_tool(db, run, ToolCall(
            id="read-note", name="read_files", arguments={"path": "note.md"},
        ), 1, client.app.state.settings)
        run.status, run.lease_token = "running", execution.token
        run.lease_until = now() + timedelta(seconds=60)
        row = db.get(UserWorkspace, run.owner_id)
        row.enabled, row.revision = False, row.revision + 1
        db.commit()
        with pytest.raises(ProviderFailure, match="workspace_changed"):
            execution.check(db, run)


@pytest.mark.parametrize("pin", [None, {}])
def test_invalid_workspace_pin_cannot_gain_a_new_binding(client, tmp_path, pin):
    """显式空绑定和损坏绑定都不能被当作新的首次读取授权。"""
    _, task = file_task(client, tmp_path)
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        run.snapshot = {**run.snapshot, "file_workspace": pin}
        with pytest.raises(ProviderFailure, match="workspace_changed"):
            execute_bound_tool(db, run, ToolCall(
                id="read-note", name="read_files", arguments={"path": "note.md"},
            ), 1, client.app.state.settings)


def test_legacy_checkpoint_file_content_never_reaches_model(client, tmp_path):
    """恢复直接进入模型节点时，也拒绝没有工作空间绑定的历史文件内容。"""
    _, task = file_task(client, tmp_path, configured=False)
    calls = []
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        run.checkpoint = {"node": "model", "state": {"messages": [
            {"role": "user", "content": "读取 note.md"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "legacy", "type": "function", "function": {
                    "name": "read_files", "arguments": '{"path":"note.md"}',
                },
            }]},
            {"role": "tool", "tool_call_id": "legacy", "content": "旧目录私有正文"},
        ], "calls": [], "text": "", "step": 1, "tool_count": 1,
            "input_tokens": 0, "output_tokens": 0, "repetitions": {}, "tool_cursor": 1}}
        db.add(ToolExecution(
            run_id=run.id, call_key="1:legacy", tool="read_files", arguments={"path": "note.md"},
            argument_hash=fingerprint("read_files", {"path": "note.md"}), status="succeeded",
            result={"content": "旧目录私有正文"},
        ))
        db.commit()
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(
        lambda *_: calls.append(True),
    )
    result = client.get(f"/api/v1/runs/{task['id']}").json()
    assert result["status"] == "failed" and result["error_code"] == "workspace_changed"
    assert not calls


def test_real_execution_reads_configured_workspace(client, tmp_path):
    """经执行图与真实账本读取夹具文件，原始目录不会作为模型参数传入。"""
    _, task = file_task(client, tmp_path)

    class FileProvider(MockProvider):
        """只模拟模型提案，文件读取和任务执行继续使用实际实现。"""

        def stream(self, messages, tools, max_tokens):
            """兼容直接和委派路径，在工具返回后生成简短答复。"""
            if messages[-1]["role"] == "tool":
                yield ModelEvent("text", text="文件读取完成")
                yield ModelEvent("reply", reply=Reply())
            elif tools == ["delegate_task"]:
                yield ModelEvent("reply", reply=Reply(calls=[ToolCall(
                    id="delegate", name="delegate_task", arguments={"task": "读取 note.md"},
                )]))
            else:
                assert "read_files" in tools
                yield ModelEvent("reply", reply=Reply(calls=[ToolCall(
                    id="read", name="read_files", arguments={"path": "note.md"},
                )]))

    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(
        lambda *_: FileProvider(),
    )
    result = client.get(f"/api/v1/runs/{task['id']}").json()
    assert result["status"] == "succeeded", result
    with Session(client.app.state.database) as db:
        item = db.scalar(select(ToolExecution).where(ToolExecution.run_id == task["id"]))
        assert item.result["content"] == "原始文件内容"
        assert item.arguments == {"path": "note.md"}
        assert db.get(Run, task["id"]).snapshot["file_workspace"]["revision"] == 1
        events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == task["id"])))
        assert any(event.kind == "text_delta" for event in events)

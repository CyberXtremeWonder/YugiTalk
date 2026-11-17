"""浏览器目录快照的上传、隔离、原子替换与工具恢复边界验证。"""

import io
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from docx import Document
from sqlalchemy import event, select
from sqlalchemy.orm import Session
from test_identity import ORIGIN, login
from test_workspace_execution import file_task

from app.core.entities import now
from app.providers.client import MockProvider, ModelEvent, ProviderFailure, Reply, ToolCall
from app.tasks.execution import Execution
from app.tasks.ledger_models import ToolExecution
from app.tasks.models import Run
from app.tasks.reliability import execute_bound_tool
from app.workspaces import imports
from app.workspaces.models import UserWorkspace

pytest_plugins = ["test_identity"]
PATH = "/api/v1/me/workspace"


@pytest.fixture
def storage(monkeypatch):
    """只在内存保存合成夹具对象，模拟真实存储接口而不访问用户资料。"""
    objects: dict[str, bytes] = {}
    fail = {"write": False, "read": False, "remove": False}

    class Storage:
        """支持注入部分上传失败、读失败和清理失败。"""

        def __init__(self, _settings):
            """测试不初始化真实对象存储连接。"""

        def put_file(self, key, source):
            """写入后再失败，覆盖上游可能已产生对象的错误场景。"""
            objects[key] = source.read()
            if fail["write"]:
                raise OSError("private storage failure")

        def get(self, key):
            """读取本次测试创建的对象，错误内容不进入公开响应。"""
            if fail["read"]:
                raise OSError("private read failure")
            return objects[key]

        def remove(self, key):
            """移除夹具对象，可模拟存储暂时不可用。"""
            if fail["remove"]:
                raise OSError("private cleanup failure")
            objects.pop(key, None)

    monkeypatch.setattr(imports, "ObjectStorage", Storage)
    monkeypatch.setattr("app.rag.storage.ObjectStorage", Storage)
    return objects, fail


def actor(client, email="owner@example.test"):
    """取得当前隔离账号的 CSRF，不由请求提供用户 ID。"""
    identity = login(client, email)
    return identity, {**ORIGIN, "X-CSRF-Token": identity["csrf_token"]}


def upload(client, headers, files=None, directory="我的资料"):
    """构造与浏览器目录选择相同的标准 multipart 请求。"""
    return client.post(PATH + "/import", data={"directory": directory}, headers=headers,
                       files=[("files", (path, content)) for path, content in (
                           files if files is not None else [("note.md", "快照正文".encode())]
                       )])


def enable(client, headers, directory="我的资料"):
    """导入后仍需由用户显式启用。"""
    response = client.put(PATH, json={"directory": directory, "enabled": True}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_import_requires_session_and_csrf_before_parsing(client, storage):
    """匿名和无防伪令牌不能产生对象或修改工作空间。"""
    assert upload(client, ORIGIN).status_code == 401
    actor(client)
    assert upload(client, ORIGIN).status_code == 403
    assert not storage[0]


def test_imported_folder_can_be_enabled_without_mounts_and_cleared(client, storage):
    """任何浏览器选定目录可变为本人快照，不依赖服务器开放目录或绝对路径。"""
    identity, headers = actor(client)
    client.app.state.settings.workspace_allowed_roots = []
    response = upload(client, headers, [("子目录/guide.md", b"example")])
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["source"] == "imported" and not data["enabled"]
    assert data["directory"] == "我的资料" and data["available"]
    assert data["file_count"] == 1 and data["total_bytes"] == 7
    assert data["files"] == [{"path": "子目录/guide.md", "size": 7}]
    assert data["imported_at"] and data["allowed_roots"] == []
    assert "key" not in response.text and str(identity["user"]["id"]) not in response.text
    enabled = enable(client, headers)
    assert enabled["enabled"] and enabled["revision"] == data["revision"] + 1
    cleared = client.put(PATH, json={"directory": "", "enabled": False}, headers=headers).json()
    assert cleared["source"] == "server" and cleared["file_count"] == 0
    assert cleared["revision"] == enabled["revision"] + 1
    assert not storage[0]
    again = upload(client, headers).json()
    assert again["revision"] == cleared["revision"] + 1


@pytest.mark.parametrize("path", [
    "../secret.md", "/secret.md", "C:/secret.md", "C:secret.md", "a\\secret.md",
    "./secret.md", "a//secret.md", "a/../secret.md", "note.md:stream", "report.doc",
    "photo.png", "a/" + "x" * 513 + ".md",
])
def test_import_rejects_ambiguous_paths_and_unsupported_files(client, storage, path):
    """文件名不用于本机落盘，仍严格拒绝路径穿越、绝对地址和未支持格式。"""
    _, headers = actor(client)
    result = upload(client, headers, [(path, b"text")])
    assert result.status_code == 422
    assert result.json()["code"] == "workspace_import_invalid"
    assert not storage[0]
    assert client.get(PATH).json()["directory"] == ""


@pytest.mark.parametrize("files", [
    [("note.md", b"one"), ("NOTE.md", b"two")],
    [("note.md", b"one"), ("note.md", b"two")],
    [("note.md", b"\xff\xfe")], [("word.docx", b"not a Word document")],
])
def test_invalid_member_cannot_replace_existing_workspace(client, storage, files):
    """一个文件无效时全批失败，先前设置、正文和对象都不变。"""
    _, headers = actor(client)
    before = upload(client, headers).json()
    before_objects = dict(storage[0])
    result = upload(client, headers, files)
    assert result.status_code == 422
    assert client.get(PATH).json() == before
    assert storage[0] == before_objects


def test_size_count_and_wire_limits_are_server_side(client, storage, monkeypatch):
    """服务端独立限制单文件、数量和无可信长度头的请求总量。"""
    _, headers = actor(client)
    assert upload(client, headers, [("large.md", b"x" * (64 * 1024 + 1))]).status_code == 413
    monkeypatch.setattr(imports, "MAX_IMPORT_FILES", 1)
    result = upload(client, headers, [("a.md", b"x"), ("b.md", b"y")])
    assert result.status_code == 413 and result.json()["code"] == "workspace_import_limit"
    monkeypatch.setattr(imports, "MAX_IMPORT_FILES", 100)
    monkeypatch.setattr(imports, "MAX_IMPORT_BYTES", 1)
    assert upload(client, headers, [("a.md", b"xy")]).status_code == 413
    monkeypatch.setattr(imports, "MAX_IMPORT_WIRE_BYTES", 100)
    result = upload(client, headers)
    assert result.status_code == 413 and not storage[0]


def test_upload_failure_preserves_old_and_removes_partial_objects(client, storage):
    """存储写入已经产生部分对象时仍回收新对象，保留已发布快照。"""
    _, headers = actor(client)
    before = upload(client, headers).json()
    old_objects = dict(storage[0])
    storage[1]["write"] = True
    result = upload(client, headers, [("new.md", b"new")], "新资料")
    assert result.status_code == 503 and result.json()["code"] == "workspace_import_unavailable"
    assert client.get(PATH).json() == before and storage[0] == old_objects


def test_truncated_multipart_never_publishes_prefix_and_closes_all_spools(
    client, storage, monkeypatch,
):
    """缺少终止边界时，已完成文件前缀也不导入，未完成文件的临时句柄同样关闭。"""
    from tempfile import SpooledTemporaryFile

    _, headers = actor(client)
    before = upload(client, headers).json()
    objects = dict(storage[0])
    spools = []

    def track_spool(*args, **kwargs):
        """记录真实临时文件，直接验证异常路径中的句柄释放。"""
        file = SpooledTemporaryFile(*args, **kwargs)
        spools.append(file)
        return file

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", track_spool)
    body = (
        b'--bound\r\nContent-Disposition: form-data; name="directory"\r\n\r\nfolder\r\n'
        b'--bound\r\nContent-Disposition: form-data; name="files"; filename="ok.md"'
        b'\r\n\r\ngood\r\n'
        b'--bound\r\nContent-Disposition: form-data; name="files"; filename="lost.md"'
        b'\r\n\r\nunfinished'
    )
    response = client.post(PATH + "/import", content=body, headers={
        **headers, "Content-Type": "multipart/form-data; boundary=bound",
    })
    assert response.status_code == 422 and len(spools) == 2
    assert all(file.closed for file in spools)
    assert client.get(PATH).json() == before and storage[0] == objects


def test_sql_failure_does_not_replace_old_and_cleans_staging(client, storage, monkeypatch):
    """SQL 提交前失败不会留下可见半份清单或孤立暂存文件。"""
    _, headers = actor(client)
    before = upload(client, headers).json()
    old_objects = dict(storage[0])

    def fail_audit(*_args):
        """在对象已上传、SQL 尚未提交时模拟数据库失败。"""
        raise RuntimeError("private SQL failure")

    monkeypatch.setattr(imports, "audit", fail_audit)
    assert upload(client, headers, [("new.md", b"new")]).status_code == 503
    assert client.get(PATH).json() == before and storage[0] == old_objects


def test_lost_commit_ack_keeps_objects_that_may_be_published(client, storage, monkeypatch):
    """数据库已提交但确认丢失时返回不确定状态，不删除当前快照需要的对象。"""
    _, headers = actor(client)
    original = Session.commit

    def persist_then_fail(db):
        """提交成功后注入一次网络确认失败。"""
        original(db)
        raise OSError("commit acknowledgement lost")

    monkeypatch.setattr(Session, "commit", persist_then_fail)
    result = upload(client, headers)
    assert result.status_code == 503 and result.json()["code"] == "workspace_import_uncertain"
    current = client.get(PATH).json()
    assert current["source"] == "imported" and current["file_count"] == 1
    assert len(storage[0]) == 1


def test_cleanup_failure_does_not_reenable_cleared_import(client, storage):
    """对象清理失败不改变已提交的撤权结果。"""
    _, headers = actor(client)
    upload(client, headers)
    enabled = enable(client, headers)
    storage[1]["remove"] = True
    response = client.put(PATH, json={"directory": "", "enabled": False}, headers=headers)
    assert response.status_code == 200
    result = response.json()
    assert not result["enabled"] and result["file_count"] == 0
    assert result["revision"] > enabled["revision"]


def test_import_user_ownership_and_same_filename_are_isolated(client, storage):
    """不同用户同名目录与文件的对象键完全独立，不能提交别人的清单。"""
    first, headers = actor(client)
    upload(client, headers)
    owner = first["user"]["id"]
    second, headers = actor(client, "other@example.test")
    assert client.get(PATH).json()["directory"] == ""
    upload(client, headers, [("note.md", b"second")])
    other = second["user"]["id"]
    with Session(client.app.state.database) as db:
        own, foreign = db.get(UserWorkspace, owner), db.get(UserWorkspace, other)
        own_key = own.files["note.md"]["key"]
        other_key = foreign.files["note.md"]["key"]
        assert own_key != other_key and f"/{owner}/" in own_key and f"/{other}/" in other_key
        own.files = {"note.md": foreign.files["note.md"]}
        with pytest.raises(ProviderFailure, match="workspace_changed"):
            imports.read_imported_file(own, "note.md", client.app.state.settings)
    forged = client.put(PATH, json={"directory": "我的资料", "enabled": True,
                                    "files": {"note.md": {"key": own_key}}}, headers=headers)
    assert forged.status_code == 422


def test_imported_word_reads_through_ledger_and_reimport_revokes_cache(client, storage, tmp_path):
    """经现有任务授权读取 Word，重导入同名目录后旧缓存和后续模型边界失效。"""
    _, task = file_task(client, tmp_path, configured=False)
    _, headers = actor(client)
    content = io.BytesIO()
    document = Document()
    document.add_paragraph("Word 快照正文")
    document.save(content)
    assert upload(client, headers, [("资料.docx", content.getvalue())]).status_code == 200
    initial = enable(client, headers)
    call = ToolCall(id="word", name="read_files", arguments={"path": "资料.docx"})
    settings = client.app.state.settings
    settings.workspace_allowed_roots = []
    execution = Execution(client.app.state.database, settings, task["id"])
    with Session(client.app.state.database) as db:
        run = db.get(Run, task["id"])
        result = execute_bound_tool(db, run, call, 1, settings)
        assert result["content"] == "Word 快照正文"
        assert run.snapshot["file_workspace"]["source"] == "imported"
        db.commit()
        assert execute_bound_tool(db, run, call, 1, settings) == result
        replacement = upload(client, headers).json()
        assert replacement["revision"] > initial["revision"] and not replacement["enabled"]
        with pytest.raises(ProviderFailure, match="workspace_changed"):
            execute_bound_tool(db, run, call, 1, settings)
        run.status, run.lease_token = "running", execution.token
        run.lease_until = now() + timedelta(seconds=60)
        db.commit()
        with pytest.raises(ProviderFailure, match="workspace_changed"):
            execution.check(db, run)


def test_imported_read_rejects_changed_or_missing_object(client, storage):
    """底层对象损坏或消失时不输出旧摘要对应之外的文件内容。"""
    identity, headers = actor(client)
    upload(client, headers)
    with Session(client.app.state.database) as db:
        row = db.get(UserWorkspace, identity["user"]["id"])
        key = row.files["note.md"]["key"]
        storage[0][key] = b"tampered"
        with pytest.raises(ProviderFailure, match="file_unavailable"):
            imports.read_imported_file(row, "note.md", client.app.state.settings)
        storage[0].clear()
        with pytest.raises(ProviderFailure, match="file_unavailable"):
            imports.read_imported_file(row, "note.md", client.app.state.settings)


def test_real_execution_uses_imported_snapshot_without_server_roots(client, storage, tmp_path):
    """模型只模拟工具提案，执行图、权限、清单解析及账本均使用真实实现。"""
    _, task = file_task(client, tmp_path, configured=False)
    _, headers = actor(client)
    upload(client, headers)
    enable(client, headers)
    client.app.state.settings.workspace_allowed_roots = []

    class FileProvider(MockProvider):
        """兼容直接执行与委派执行，实际文件读取由内置工具完成。"""

        def stream(self, messages, tools, max_tokens):
            """工具回包后生成固定答复，否则提出相对文件路径。"""
            if messages[-1]["role"] == "tool":
                yield ModelEvent("text", text="快照读取完成")
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
        assert db.get(Run, task["id"]).snapshot["file_workspace"]["source"] == "imported"


@pytest.mark.parametrize("action", ["cancel", "reimport"])
def test_slow_snapshot_read_releases_transaction_and_rechecks_changes(
    client, storage, tmp_path, monkeypatch, action,
):
    """慢 S3 读取期间不持有任务事务，取消和重导入都能生效，旧读取结果不能提交。"""
    _, task = file_task(client, tmp_path, configured=False)
    _, headers = actor(client)
    upload(client, headers)
    enable(client, headers)
    engine = client.app.state.database
    execution = Execution(engine, client.app.state.settings, task["id"])
    with Session(engine) as db:
        run = db.get(Run, task["id"])
        run.status, run.lease_token = "running", execution.token
        run.lease_until = now() + timedelta(seconds=60)
        db.commit()
    active = set()
    event.listen(engine, "begin", lambda connection: active.add(id(connection)))
    event.listen(engine, "commit", lambda connection: active.discard(id(connection)))
    event.listen(engine, "rollback", lambda connection: active.discard(id(connection)))
    started, release = Event(), Event()
    observed = []
    original = imports.ObjectStorage.get

    def slow_get(storage_instance, key):
        """以可控等待模拟慢存储，并记录读取开始时是否仍存在 SQL 事务。"""
        content = original(storage_instance, key)
        observed.append(bool(active))
        started.set()
        assert release.wait(10), "test did not release storage read"
        return content

    monkeypatch.setattr(imports.ObjectStorage, "get", slow_get)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execution.tool, ToolCall(
            id="slow", name="read_files", arguments={"path": "note.md"},
        ), 1)
        try:
            assert started.wait(5), "storage read did not begin"
            assert observed == [False]
            if action == "cancel":
                assert client.post(f"/api/v1/runs/{task['id']}/cancel",
                                   headers=headers).status_code == 200
            else:
                assert upload(client, headers, [("note.md", b"replacement")]).status_code == 200
        finally:
            release.set()
        with pytest.raises(ProviderFailure, match="cancelled|workspace_changed"):
            future.result(timeout=5)
    with Session(engine) as db:
        item = db.scalar(select(ToolExecution).where(ToolExecution.run_id == task["id"]))
        assert item.status != "succeeded" and not item.result

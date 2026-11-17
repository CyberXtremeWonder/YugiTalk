"""附件工具的内容、网络边界、执行账本与私有下载回归。"""

import io
import time
import zipfile

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_agents import setup_task
from test_identity import login
from test_subagents import Script, tool

from app.providers.client import ProviderFailure, ToolCall
from app.research.conversation import export_only_request
from app.research.models import ResearchProject
from app.tasks.execution import Execution
from app.tasks.ledger_models import ToolExecution
from app.tools.artifacts import build_files
from app.tools.builtin.archives import archive
from app.tools.http import pictures

pytest_plugins = ["test_identity"]


def test_zip_request_keeps_artifact_tools_available():
    assert not export_only_request("把这段内容整理成 Markdown 并打包 ZIP")


def test_zip_title_does_not_duplicate_extension():
    files = build_files(
        "create_zip",
        {"title": "测试.ZIP", "files": [{"filename": "hello.txt", "content": "hello"}]},
        lambda: None,
    )
    assert files[0][0] == "测试.zip"


def png():
    output = io.BytesIO()
    Image.new("RGB", (2, 3), "blue").save(output, format="PNG")
    return output.getvalue()


@pytest.mark.parametrize("name", ["../x", "/x", "a\\x", "C:/x", "x\x00", "a/./x", "a//x"])
def test_zip_rejects_unsafe_paths(name):
    with pytest.raises(ValueError):
        archive([(name, b"text")])


def test_zip_preserves_unicode_and_rejects_case_duplicates():
    content = archive([("资料/中文.txt", "你好".encode())])
    with zipfile.ZipFile(io.BytesIO(content)) as zipped:
        assert zipped.read("资料/中文.txt").decode() == "你好"
    with pytest.raises(ValueError):
        archive([("A.txt", b"a"), ("a.txt", b"b")])
    with pytest.raises(ValueError):
        archive([])


@pytest.mark.parametrize("data", [b"<html>not an image", b"<svg></svg>", b"\x89PNG\r\n\x1a\n"])
def test_non_images_rejected(data):
    with pytest.raises(ProviderFailure, match="tool_response_invalid"):
        pictures.picture(data, 1)


def test_crawl_static_page_resolves_relative_urls_and_skips_invalid(monkeypatch):
    requested = []

    def read(url, guard, deadline):
        requested.append(url)
        if url.endswith("/page"):
            return (
                b'<img src="/bad"><img data-src="/good.png"><img src="/good.png">',
                "text/html",
                "https://example.test/page",
            )
        return (png() if url.endswith(".png") else b"not-image", "image/png", url)

    monkeypatch.setattr(pictures, "read_url", read)
    files = pictures.fetch_picture({"url": "https://example.test/page"}, lambda: None)
    assert files == [("image-1.png", "image/png", png())]
    assert requested == [
        "https://example.test/page",
        "https://example.test/bad",
        "https://example.test/good.png",
    ]
    zipped = build_files(
        "fetch_picture", {"url": "https://example.test/page", "as_zip": True}, lambda: None
    )
    with zipfile.ZipFile(io.BytesIO(zipped[0][2])) as result:
        assert result.read("image-1.png") == png()


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test/a",
        "file:///x",
        "https://u:p@example.test/a",
        "https://example.test:8080/a",
    ],
)
def test_url_validation_happens_before_connection(url, monkeypatch):
    def forbidden(*args):
        pytest.fail("unsafe URL must not reach DNS")

    monkeypatch.setattr(pictures, "resolve_public", forbidden)
    with pytest.raises(ProviderFailure, match="tool_network_denied"):
        pictures.read_url(url, lambda: None, time.monotonic() + 2)


def network(monkeypatch, responses):
    """使用真实下载循环，替换已固定地址的连接边界。"""

    class Connection:
        sock = None

        def __init__(self, host, address, timeout):
            self.response = next(responses)

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return self.response

        def close(self):
            pass

    monkeypatch.setattr(pictures, "PinnedHTTPSConnection", Connection)


class Response:
    def __init__(self, data=b"", status=200, **headers):
        self.status = status
        self.headers = headers
        self.body = io.BytesIO(data)

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read1(self, size):
        return self.body.read(size)


def test_private_redirect_rechecks_dns(monkeypatch):
    hosts = []

    def resolve(host, deadline):
        hosts.append(host)
        if host == "127.0.0.1":
            raise ProviderFailure("tool_network_denied")
        return "1.1.1.1"

    monkeypatch.setattr(pictures, "resolve_public", resolve)
    network(monkeypatch, iter([Response(status=302, Location="https://127.0.0.1/secret")]))
    with pytest.raises(ProviderFailure, match="tool_network_denied"):
        pictures.read_url("https://example.test/a", lambda: None, time.monotonic() + 2)
    assert hosts == ["example.test", "127.0.0.1"]


@pytest.mark.parametrize("url", ["https://127.0.0.1/a", "https://[::1]/a"])
def test_private_addresses_never_connect(url, monkeypatch):
    def forbidden(*args):
        pytest.fail("private destination must not connect")

    monkeypatch.setattr(pictures, "PinnedHTTPSConnection", forbidden)
    with pytest.raises(ProviderFailure, match="tool_network_denied"):
        pictures.read_url(url, lambda: None, time.monotonic() + 5)


def test_network_download_size_and_actual_image_validation(monkeypatch):
    monkeypatch.setattr(pictures, "resolve_public", lambda *args: "1.1.1.1")
    network(
        monkeypatch,
        iter(
            [
                Response(png(), **{"Content-Type": "image/png"}),
                Response(b"x" * (pictures.LIMIT + 1)),
            ]
        ),
    )
    files = pictures.fetch_picture({"url": "https://example.test/a"}, lambda: None)
    assert files[0][2] == png()
    with pytest.raises(ProviderFailure, match="tool_result_too_large"):
        pictures.read_url("https://example.test/a", lambda: None, time.monotonic() + 2)


def test_cancellation_not_swallowed_while_crawling(monkeypatch):
    monkeypatch.setattr(
        pictures, "read_url", lambda *args: (b'<img src="/x">', "text/html", "https://example.test")
    )

    def cancelled():
        raise ProviderFailure("cancelled")

    with pytest.raises(ProviderFailure, match="cancelled"):
        pictures.fetch_picture({"url": "https://example.test"}, cancelled)


def test_cancel_during_download_does_not_save_attachments(client, monkeypatch):
    headers, _, task, _ = setup_task(client, "请下载图片", tools=["fetch_picture"])

    def read(*args):
        response = client.post(f"/api/v1/runs/{task['id']}/cancel", headers=headers)
        assert response.status_code == 200
        return png(), "image/png", args[0]

    monkeypatch.setattr(pictures, "read_url", read)
    provider = Script(
        [tool("fetch_picture", url="https://example.test/a.png")],
        [
            "fetch_picture",
            "list_knowledge_bases",
            "knowledge_search",
            "research_report",
            "export_document",
        ],
    )
    Execution(client.app.state.database, client.app.state.settings, task["id"]).execute(
        lambda *args: provider
    )
    assert client.get(f"/api/v1/runs/{task['id']}").json()["status"] == "cancelled"
    with Session(client.app.state.database) as db:
        assert db.scalar(select(ResearchProject)) is None


@pytest.mark.parametrize("name", ["create_zip", "fetch_picture"])
def test_agent_delivers_private_downloadable_files_and_reuses_ledger(client, monkeypatch, name):
    _, body, task, _ = setup_task(client, "请交付文件附件", tools=[name])
    monkeypatch.setattr(pictures, "read_url", lambda *args: (png(), "image/png", args[0]))
    arguments = (
        {
            "files": [{"filename": "说明.txt", "content": "中文说明"}],
            "image_urls": ["https://example.test/a.png"],
        }
        if name == "create_zip"
        else {"url": "https://example.test/a.png"}
    )
    execution = Execution(client.app.state.database, client.app.state.settings, task["id"])

    def confirm(messages):
        with Session(client.app.state.database) as db:
            item = db.scalar(select(ToolExecution))
            step, call_id = item.call_key.split(":", 1)
            cached = dict(item.result)
        assert (
            execution.tool(ToolCall(id=call_id, name=name, arguments=arguments), int(step))
            == cached
        )
        with pytest.raises(ProviderFailure, match="tool_not_authorized"):
            execution.tool(ToolCall(id="forbidden", name="text_stats", arguments={"text": "x"}), 0)
        return "请下载附件。"

    provider = Script(
        [tool(name, **arguments), confirm],
        [name, "list_knowledge_bases", "knowledge_search", "research_report", "export_document"],
    )
    execution.execute(lambda *args: provider)
    assert client.get(f"/api/v1/runs/{task['id']}").json()["status"] == "succeeded"
    history = client.get(f"/api/v1/conversations/{body['conversation_id']}/messages").json()
    file = history["items"][-1]["attachments"][0]
    response = client.get(file["download_url"])
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    if name == "create_zip":
        with zipfile.ZipFile(io.BytesIO(response.content)) as result:
            assert result.read("说明.txt").decode() == "中文说明"
            assert result.read("image-1.png") == png()
    else:
        assert response.content == png()
    with Session(client.app.state.database) as db:
        item = db.scalar(select(ToolExecution))
        assert item.status == "succeeded"
        assert "content" not in item.result["files"][0]
        if name == "create_zip":
            assert item.result["files"][0]["entries"] == ["说明.txt", "image-1.png"]
        assert len(list(db.scalars(select(ResearchProject)))) == 1
    login(client, "other@example.test")
    assert client.get(file["download_url"]).status_code == 404

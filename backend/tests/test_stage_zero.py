"""验证阶段 0 的参数边界、审批恢复与检查点隔离。"""

from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from probes.contracts import validate_proposal
from probes.graph import build_graph
from pydantic import ValidationError


def test_valid_tool_proposal() -> None:
    """验证符合结构化契约的工具提案可以通过校验。"""
    proposal = validate_proposal(
        {"name": "format_report", "arguments": {"title": "演示", "body": "测试"}}
    )
    assert proposal.arguments.title == "演示"


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "delete_database", "arguments": {"title": "x", "body": "x"}},
        {"name": "format_report", "arguments": {"title": "", "body": "x"}},
        {"name": "format_report", "arguments": {"title": 7, "body": "x"}},
        {
            "name": "format_report",
            "arguments": {"title": "x", "body": "x", "account_id": "other"},
        },
        "not-json",
    ],
)
def test_invalid_proposal_never_passes_validation(payload: object) -> None:
    """验证非法工具提案始终被契约校验拒绝。"""
    with pytest.raises(ValidationError):
        validate_proposal(payload)


@pytest.mark.parametrize("decision,expected", [("approve", "approved"), ("reject", "rejected")])
def test_checkpoint_survives_saver_recreation(tmp_path: Path, decision: str, expected: str) -> None:
    """验证重新创建存储器后仍可读取持久检查点。"""
    path = str(tmp_path / "checkpoint.sqlite")
    config = {"configurable": {"thread_id": "recoverable"}}
    with SqliteSaver.from_conn_string(path) as saver:
        graph = build_graph(saver)
        graph.invoke({"prompt": "预览演示报告"}, config)
        assert graph.get_state(config).next == ("approval",)

    # 重建存储器与执行图，模拟新进程恢复，避免误把内存续跑当作持久化恢复。
    with SqliteSaver.from_conn_string(path) as saver:
        graph = build_graph(saver)
        graph.invoke(Command(resume=decision), config)
        snapshot = graph.get_state(config)
        assert snapshot.values["status"] == expected
        assert snapshot.next == ()


def test_threads_do_not_share_checkpoint_state(tmp_path: Path) -> None:
    """验证不同任务线程不会混用检查点状态。"""
    with SqliteSaver.from_conn_string(str(tmp_path / "isolated.sqlite")) as saver:
        graph = build_graph(saver)
        graph.invoke({"prompt": "thread-a"}, {"configurable": {"thread_id": "a"}})
        empty = graph.get_state({"configurable": {"thread_id": "b"}})
        assert empty.values == {}

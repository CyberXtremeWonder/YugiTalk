"""验证提示词按需读取，无缓存且不受进程工作目录影响。"""

import pytest

from app.agents.answer_policy import answer_instruction
from app.rag import prompts


def test_only_requested_prompt_is_read_and_changes_are_visible(tmp_path, monkeypatch):
    """其他提示词缺失不影响当前功能，下次调用读取修改后的内容。"""
    path = tmp_path / "plan.md"
    path.write_text("first\n", encoding="utf-8")
    monkeypatch.setattr(prompts, "_FILES", {"plan": path, "grade": tmp_path / "missing.md"})
    monkeypatch.chdir(tmp_path)
    assert prompts.load_prompt("plan") == "first"
    path.write_text("second\n", encoding="utf-8")
    assert prompts.load_prompt("plan") == "second"
    with pytest.raises(FileNotFoundError):
        prompts.load_prompt("grade")
    with pytest.raises(KeyError):
        prompts.load_prompt("../plan")


@pytest.mark.parametrize("coverage,expected", [
    ("none", "本次检索无有效证据"),
    ("partial", "资料仅部分支持问题"),
    ("sufficient", "仅依据有效资料回答当前问题"),
])
def test_answer_branches_keep_their_constraints(coverage, expected):
    """证据覆盖分支与原有引用和检索约束保持一致。"""
    text = answer_instruction({"coverage": coverage})
    assert text.startswith(expected)
    assert "不编造 evidence_id" in text
    assert "不重复相同查询" in text

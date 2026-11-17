"""在功能调用时读取对应的本地提示词，不预加载或缓存正文。"""

from pathlib import Path

_DIRECTORY = Path(__file__).resolve().parent
_FILES = {name: _DIRECTORY / (name + ".md") for name in (
    "plan", "grade", "rewrite", "graph", "answer_none", "answer_partial", "answer_sufficient",
)}


def load_prompt(name: str) -> str:
    """名称只允许固定映射；缺失文件直接报错，避免无声使用旧指令。"""
    return _FILES[name].read_text(encoding="utf-8").strip()

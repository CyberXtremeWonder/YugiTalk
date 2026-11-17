"""本地内置工具分发；HTTP 和 MCP 由任务执行层分流到各自运行模块。"""

from pathlib import Path

from app.providers.client import ProviderFailure
from app.tools.builtin.files import read_files
from app.tools.builtin.text import format_report, text_stats


def execute_tool(
    name: str, arguments: object, *, workspace: Path | None = None
) -> dict[str, object]:
    """仅分发固定内置工具，文件根目录必须由已鉴权的执行层传入。"""
    if name == "text_stats":
        return text_stats(arguments)
    if name == "format_report":
        return format_report(arguments)
    if name == "read_files":
        if workspace is None:
            raise ProviderFailure("workspace_not_configured")
        return read_files(arguments, workspace)
    raise ValueError("工具未注册")

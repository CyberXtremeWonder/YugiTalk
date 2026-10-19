"""工具执行入口的确定性授权，模型文本或成功账本不能授予额外权限。"""

from typing import Any

from app.providers.client import ProviderFailure


def authorize_tool(snapshot: dict[str, Any], name: str) -> None:
    """执行和复用结果前再次检查任务许可及技能交集，缺失许可时默认拒绝。"""
    tools = snapshot.get("tools")
    if not isinstance(tools, list) or name not in tools:
        raise ProviderFailure("tool_not_authorized")
    pin = snapshot.get("skill")
    if pin is not None and (
        not isinstance(pin, dict)
        or not isinstance(pin.get("allowed_tools"), list)
        or name not in pin["allowed_tools"]
    ):
        raise ProviderFailure("tool_not_authorized")

"""公开工具元数据与固定许可注册表。"""

from app.tools.contracts import Report, TextStats
from app.tools.default_http import DEFAULT_HTTP_NAMES, default_config
from app.tools.web_search import WebSearch, search_config

TOOL_SCHEMAS: dict[str, dict[str, object]] = {
    "text_stats": {
        "name": "text_stats",
        "description": "统计给定文本的字符数与非空行数，只读操作",
        "parameters": TextStats.model_json_schema(),
    },
    "format_report": {
        "name": "format_report",
        "description": "将给定标题和内容整理为 Markdown 报告，不写文件或发送消息",
        "parameters": Report.model_json_schema(),
    },
}

for _name in sorted(DEFAULT_HTTP_NAMES):
    _config = default_config(_name)
    TOOL_SCHEMAS[_name] = {
        "name": _name,
        "description": _config["description"],
        "parameters": {
            "type": "object",
            "properties": {
                item["name"]: {"type": item["type"], "description": item["description"]}
                for item in _config["parameters"]
            },
            "required": [item["name"] for item in _config["parameters"] if item["required"]],
            "additionalProperties": False,
        },
    }

TOOL_SCHEMAS["web_search"] = {
    "name": "web_search",
    "description": search_config()["description"],
    "parameters": WebSearch.model_json_schema(),
}

"""公开工具元数据与固定许可注册表。"""

from app.tools.builtin.archives import CreateZip
from app.tools.builtin.contracts import ReadFiles, Report, TextStats
from app.tools.http.defaults import DEFAULT_HTTP_NAMES, default_config
from app.tools.http.pictures_contract import FetchPicture
from app.tools.http.search import WebSearch, search_config
from app.tools.http.torrent_contract import DownloadTorrent

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
TOOL_SCHEMAS["read_files"] = {
    "name": "read_files",
    "description": (
        "读取当前用户工作空间中的 .txt、.md 或 Word .docx 正文与表格，"
        "返回内容最多 64 KiB；Word 文件最多 10 MiB，旧 .doc 需先转换为 .docx"
    ),
    "parameters": ReadFiles.model_json_schema(),
}
TOOL_SCHEMAS["create_zip"] = {
    "name": "create_zip",
    "description": "将给定文本文件和可选 HTTPS 图片直链打包为可下载 ZIP 附件；不读取服务器路径。",
    "parameters": CreateZip.model_json_schema(),
}
TOOL_SCHEMAS["fetch_picture"] = {
    "name": "fetch_picture",
    "description": (
        "下载 HTTPS 图片直链，或抓取静态网页 img 标签中的图片，返回聊天下载附件。"
        "最多 6 张、单张 5 MiB、合计 15 MiB；支持 PNG/JPEG/WebP/GIF，不执行网页脚本。"
    ),
    "parameters": FetchPicture.model_json_schema(),
}
TOOL_SCHEMAS["download_torrent"] = {
    "name": "download_torrent",
    "description": (
        "通过后台 aria2 下载磁力链接或 HTTPS .torrent 直链中的文件。"
        "action=start 提交 source；action=status 用返回的 download_id 查询进度。"
        "submitted 仅表示已提交，只有 complete 表示下载完成。"
        "文件保存在下载服务器，不生成聊天下载附件；勿在同次回答中循环轮询。"
    ),
    "parameters": DownloadTorrent.model_json_schema(),
}

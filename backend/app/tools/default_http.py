"""固定天气与路况查询契约；服务密钥由执行层注入，绝不进入模型参数。"""

from typing import Any


def default_config(name: str) -> dict[str, Any]:
    """生成受服务端代码控制的高德只读接口声明，不允许页面替换内置端点。"""
    weather = name == "weather_query"
    return {
        "name": name,
        "title": "天气查询" if weather else "路况查询",
        "description": (
            "查询中国城市实况天气或天气预报。city 必须是六位城市行政编码；"
            "extensions 可选 base（实况）或 all（预报），以服务返回的更新时间为准。"
            if weather
            else "查询指定城市道路的实时交通态势。name 为道路名称，adcode 为六位市级编码；"
            "结果只描述查询时刻的道路状况，不是导航路线或到达时间预测。"
        ),
        "endpoint": "https://restapi.amap.com/v3/"
        + ("weather/weatherInfo" if weather else "traffic/status/road"),
        "method": "GET",
        "parameters": [
            {"name": "city", "type": "string", "description": "六位行政区划编码", "required": True},
            {
                "name": "extensions",
                "type": "string",
                "description": "base 或 all",
                "required": False,
            },
        ]
        if weather
        else [
            {"name": "name", "type": "string", "description": "道路名称", "required": True},
            {
                "name": "adcode",
                "type": "string",
                "description": "六位市级行政区划编码",
                "required": True,
            },
        ],
        "required_roles": ["owner", "admin", "member"],
        "secret_name": "AMAP_API_KEY",
        "timeout_seconds": 10,
        "max_response_bytes": 16000,
    }


DEFAULT_HTTP_NAMES = {"weather_query", "traffic_query"}

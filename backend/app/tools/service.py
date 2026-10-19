"""统一空间工具目录、输入 Schema、版本冻结与调用时的确定性授权。"""

import hashlib
import json
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounts.access import membership
from app.core.config import Settings
from app.providers.client import ProviderFailure
from app.tools.default_http import DEFAULT_HTTP_NAMES, default_config
from app.tools.management_contracts import ToolInput
from app.tools.models import ManagedTool, ToolVersion
from app.tools.registry import TOOL_SCHEMAS
from app.tools.web_search import search_config

TITLES = {
    "web_search": "网页搜索",
    "text_stats": "文本统计",
    "format_report": "报告排版",
    "weather_query": "天气查询",
    "traffic_query": "路况查询",
}


def secret_value(config: dict[str, Any], settings: Settings) -> str:
    """仅执行层取出密钥；高德专用引用只能用于固定高德服务域名。"""
    name = config.get("secret_name", "")
    if name == "TAVILY_API_KEY":
        return (
            settings.tavily_api_key.get_secret_value()
            if config.get("endpoint") == "https://api.tavily.com/search" else ""
        )
    if name == "AMAP_API_KEY":
        return (
            settings.amap_api_key.get_secret_value()
            if urlsplit(config["endpoint"]).hostname == "restapi.amap.com"
            else ""
        )
    value = settings.tool_secrets.get(name)
    return value.get_secret_value() if value else ""


def digest(config: dict[str, Any]) -> str:
    """绑定规范 JSON 内容，防止检查点或数据库版本被原地改写后继续使用。"""
    return hashlib.sha256(
        json.dumps(
            config, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def input_schema(config: dict[str, Any]) -> dict[str, Any]:
    """从有界标量声明构建模型工具 Schema，不加载远端引用或执行代码。"""
    properties = {}
    for item in config["parameters"]:
        value: dict[str, Any] = {"type": item["type"], "description": item["description"]}
        if item["type"] == "string":
            value["maxLength"] = 2000
        properties[item["name"]] = value
    return {
        "type": "object",
        "properties": properties,
        "required": [item["name"] for item in config["parameters"] if item["required"]],
        "additionalProperties": False,
    }


def find_tool(db: Session, workspace: str, name: str) -> ManagedTool | None:
    """按空间和未删除名称查找工具，不复用其他空间的同名配置。"""
    return db.scalar(
        select(ManagedTool).where(
            ManagedTool.account_id == workspace,
            ManagedTool.name == name,
            ManagedTool.deleted.is_(False),
        )
    )


def latest(db: Session, tool: ManagedTool) -> ToolVersion:
    """读取最新不可变版本，缺失有效版本时默认拒绝。"""
    version = db.scalar(
        select(ToolVersion)
        .where(ToolVersion.tool_id == tool.id)
        .order_by(ToolVersion.number.desc())
        .limit(1)
    )
    if version is None:
        raise HTTPException(409, detail={"code": "tool_unavailable"})
    return version


def readiness(config: dict[str, Any], settings: Settings) -> str:
    """只返回缺失配置的说明，绝不返回服务端密钥值。"""
    host = urlsplit(config["endpoint"]).hostname
    if host not in settings.tool_http_allowed_hosts:
        return "尚未配置服务端域名白名单"
    name = config.get("secret_name")
    if name and not secret_value(config, settings):
        return "尚未配置服务端密钥引用"
    return ""


def descriptor(db: Session, workspace: str, name: str) -> dict[str, Any] | None:
    """读取工具的不可变声明，内置工具默认启用且不因读取而写数据库。"""
    row = find_tool(db, workspace, name)
    if name in TOOL_SCHEMAS:
        schema = TOOL_SCHEMAS[name]
        item: dict[str, Any] = {
            "name": name,
            "title": TITLES[name],
            "description": schema["description"],
            "kind": "builtin",
            "builtin": True,
            "number": 1,
            "enabled": row.enabled if row else True,
            "required_roles": ["owner", "admin", "member"],
            "schema": schema,
            "content_hash": digest(schema),
        }
        if name in DEFAULT_HTTP_NAMES or name == "web_search":
            item["kind"] = "http"
            item["config"] = search_config() if name == "web_search" else default_config(name)
            item["content_hash"] = digest({"schema": schema, "config": item["config"]})
        return item
    if row is None or row.builtin:
        return None
    version = latest(db, row)
    return {
        "name": name,
        "title": version.config["title"],
        "description": version.config["description"],
        "kind": "http",
        "builtin": False,
        "owner_id": row.owner_id,
        "id": row.id,
        "number": version.number,
        "version_id": version.id,
        "enabled": row.enabled,
        "required_roles": version.config["required_roles"],
        "schema": {
            "name": name,
            "description": version.config["description"],
            "parameters": input_schema(version.config),
        },
        "config": version.config,
        "content_hash": version.content_hash,
    }


def validate_names(db: Session, workspace: str, names: list[str], user: str | None = None) -> None:
    """发布助手或技能时拒绝未知工具，不能通过任意名称制造动态执行入口。"""
    for name in names:
        item = descriptor(db, workspace, name)
        if item is None or (item.get("owner_id") and item["owner_id"] != user):
            raise HTTPException(422, detail={"code": "tool_not_registered"})


def freeze_tools(
    db: Session, workspace: str, user: str, names: list[str], settings: Settings
) -> dict[str, Any]:
    """任务只冻结当前启用、角色允许且配置就绪的工具；空集合表示无工具权限。"""
    member = membership(db, workspace, user)
    if member is None:
        raise HTTPException(404)
    pins = {}
    for name in names:
        item = descriptor(db, workspace, name)
        if item and item["enabled"] and member.role in item["required_roles"]:
            if item.get("owner_id") and item["owner_id"] != user:
                continue
            if item["kind"] == "http" and readiness(item["config"], settings):
                continue
            pins[name] = item
    return pins


def bound_tool(
    db: Session,
    workspace: str,
    user: str,
    snapshot: dict[str, Any],
    name: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """执行与缓存复用之前复核当前启停、角色、原版本哈希，旧任务只兼容内置工具。"""
    if workspace != user:
        raise ProviderFailure("permission_revoked")
    member = membership(db, workspace, user)
    row = find_tool(db, workspace, name)
    if row is not None and row.owner_id and row.owner_id != user:
        raise ProviderFailure("tool_not_authorized")
    if row is not None and not row.enabled:
        raise ProviderFailure("tool_unavailable")
    pins = snapshot.get("tool_catalog")
    pin = pins.get(name) if isinstance(pins, dict) else None
    if isinstance(pins, dict) and not isinstance(pin, dict):
        raise ProviderFailure("tool_not_authorized")
    if name in TOOL_SCHEMAS:
        item = descriptor(db, workspace, name)
        assert item is not None
        if pin is not None and (
            pin.get("content_hash") != item["content_hash"]
            or pin.get("schema") != item["schema"]
            or pin.get("config") != item.get("config")
        ):
            raise ProviderFailure("tool_content_changed")
        if (name in DEFAULT_HTTP_NAMES or name == "web_search") and (
            pin is None or settings is None or readiness(item["config"], settings)
        ):
            raise ProviderFailure("tool_not_configured")
        return item
    if not isinstance(pin, dict) or not row or row.id != pin.get("id") or row.deleted:
        raise ProviderFailure("tool_unavailable")
    version = db.get(ToolVersion, pin.get("version_id"))
    if (
        not version
        or version.tool_id != row.id
        or version.content_hash != pin.get("content_hash")
        or digest(version.config) != version.content_hash
        or version.config != pin.get("config")
        or pin.get("name") != name
        or pin.get("number") != version.number
        or pin.get("owner_id") != row.owner_id
        or pin.get("schema")
        != {
            "name": name,
            "description": version.config["description"],
            "parameters": input_schema(version.config),
        }
    ):
        raise ProviderFailure("tool_content_changed")
    # 新版角色策略可收窄旧版本授权，但不把新端点替换到旧任务中。
    current = latest(db, row)
    if (
        member.role not in version.config["required_roles"]
        or member.role not in current.config["required_roles"]
    ):
        raise ProviderFailure("tool_not_authorized")
    if settings is None or readiness(version.config, settings):
        raise ProviderFailure("tool_not_configured")
    return pin


def check_tools(
    db: Session, workspace: str, user: str, snapshot: dict[str, Any], settings: Settings
) -> None:
    """在模型调用、恢复和每个公开增量边界重验工具撤销，不允许检查点复活旧权限。"""
    for name in snapshot.get("tools", []):
        bound_tool(db, workspace, user, snapshot, name, settings)


def validate_config(config: dict[str, Any]) -> ToolInput:
    """对恢复的声明重新执行静态契约校验，防止数据库字段绕过网络格式限制。"""
    return ToolInput.model_validate(config)


def check_resume_tools(
    db: Session, workspace: str, user: str, snapshot: dict[str, Any], settings: Settings
) -> None:
    """恢复前先校验工具撤销，失败不新增预留、恢复记录或重复外部请求。"""
    try:
        check_tools(db, workspace, user, snapshot, settings)
    except ProviderFailure as error:
        raise HTTPException(409, detail={"code": error.code}) from None

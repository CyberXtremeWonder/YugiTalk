"""MCP 工具注册、用户授权和仅执行时解密的凭证。"""

import json
from typing import Any

from cryptography.fernet import InvalidToken
from jsonschema import Draft202012Validator
from sqlalchemy.orm import Session

from app.agents.model_config import cipher
from app.core.config import Settings
from app.providers.client import ProviderFailure
from app.tools.mcp.client import MCPClient
from app.tools.mcp.models import MCPCredential, MCPServer, MCPServerVersion
from app.tools.models import ManagedTool, ToolVersion
from app.tools.service import digest


def token_value(db: Session, server: MCPServer, settings: Settings) -> str:
    row = db.get(MCPCredential, server.id)
    if row is None:
        return ""
    try:
        value = json.loads(cipher(settings).decrypt(row.encrypted.encode()))
        if value["user"] != server.account_id or value["server"] != server.id:
            raise ValueError("credential owner")
        return str(value["token"])
    except (InvalidToken, ValueError, KeyError, TypeError) as error:
        raise ProviderFailure("mcp_credentials_invalid") from error


def checked_tools(value: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    names = set()
    for tool in value:
        if not isinstance(tool, dict):
            raise ValueError("MCP 工具声明无效")
        name = tool.get("name")
        schema = tool.get("inputSchema")
        if not isinstance(name, str) or not 1 <= len(name) <= 128 or name in names:
            raise ValueError("MCP 工具名称无效或重复")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError("MCP 工具参数必须为对象 Schema")

        # 不解析远程引用；本地 $defs 引用由 JSON Schema 校验器处理。
        def check(node: Any, depth: int = 0) -> None:
            if depth > 20:
                raise ValueError("MCP Schema 嵌套过深")
            if isinstance(node, dict):
                for key, item in node.items():
                    if key in {"$ref", "$dynamicRef"} and (
                        not isinstance(item, str) or not item.startswith("#")
                    ):
                        raise ValueError("不支持外部 Schema 引用")
                    if key == "$id":
                        raise ValueError("不支持 Schema $id")
                    check(item, depth + 1)
            elif isinstance(node, list):
                for item in node:
                    check(item, depth + 1)

        if len(json.dumps(schema)) > 16000:
            raise ValueError("MCP Schema 过大")
        check(schema)
        Draft202012Validator.check_schema(schema)
        description = tool.get("description", name)
        title = tool.get("title", name)
        if not isinstance(description, str) or not isinstance(title, str):
            raise ValueError("MCP 工具说明无效")
        result.append(
            {
                "name": name,
                "title": title[:80],
                "description": description[:500],
                "inputSchema": schema,
            }
        )
        names.add(name)
    return result


def entry(db: Session, row: ManagedTool, version: ToolVersion) -> dict[str, Any]:
    config = version.config
    server = db.get(MCPServer, config["server_id"])
    available = bool(
        server
        and server.account_id == row.account_id
        and not server.deleted
        and server.enabled
        and server.revision == config["server_revision"]
    )
    return {
        "name": row.name,
        "title": config["title"],
        "description": config["description"],
        "kind": "mcp",
        "builtin": False,
        "owner_id": row.owner_id,
        "id": row.id,
        "number": version.number,
        "version_id": version.id,
        "enabled": row.enabled and available,
        "required_roles": ["owner"],
        "schema": {
            "name": row.name,
            "description": config["description"],
            "parameters": config["inputSchema"],
        },
        "config": config,
        "content_hash": version.content_hash,
    }


def bound(db: Session, user: str, pin: dict[str, Any], name: str) -> dict[str, Any]:
    row = db.get(ManagedTool, pin.get("id"))
    version = db.get(ToolVersion, pin.get("version_id"))
    if (
        not row
        or row.account_id != user
        or row.owner_id != user
        or row.deleted
        or not version
        or version.tool_id != row.id
        or row.name != name
        or version.config.get("kind") != "mcp"
    ):
        raise ProviderFailure("tool_not_authorized")
    actual = entry(db, row, version)
    if not actual["enabled"]:
        raise ProviderFailure("tool_unavailable")
    if actual != pin or digest(version.config) != version.content_hash:
        raise ProviderFailure("tool_content_changed")
    return actual


def execute(
    engine: Any,
    user: str,
    pin: dict[str, Any],
    arguments: dict[str, Any],
    settings: Settings,
    guard: Any,
) -> dict[str, object]:
    with Session(engine) as db:
        bound(db, user, pin, pin["name"])
        server = db.get(MCPServer, pin["config"]["server_id"])
        assert server is not None
        version = db.get(MCPServerVersion, pin["config"]["server_version_id"])
        if not version or version.server_id != server.id:
            raise ProviderFailure("mcp_configuration_invalid")
        config, token = version.config, token_value(db, server, settings)
    try:
        if len(json.dumps(arguments)) > 16000:
            raise ValueError("arguments too large")
        Draft202012Validator(pin["config"]["inputSchema"]).validate(arguments)
    except Exception as error:
        raise ProviderFailure("mcp_arguments_invalid") from error

    def check() -> None:
        guard()
        with Session(engine) as db:
            bound(db, user, pin, pin["name"])

    try:
        with MCPClient(config, token, check) as client:
            # Schema 变化需要重新发现和授权；不能静默调用被远端替换的声明。
            remote = checked_tools(client.tools())
            tool = next((t for t in remote if t["name"] == pin["config"]["remote_name"]), None)
            if (
                not tool
                or tool["inputSchema"] != pin["config"]["inputSchema"]
                or tool["description"] != pin["config"]["description"]
            ):
                raise ProviderFailure("tool_content_changed")
            result = client.rpc("tools/call", {"name": tool["name"], "arguments": arguments})
            check()
            if len(json.dumps(result)) > 16000:
                return {"ok": False, "error_code": "mcp_result_too_large"}
            return {"ok": not result.get("isError", False), "data": result}
    except ProviderFailure:
        raise
    except Exception as error:
        raise ProviderFailure("mcp_call_failed") from error

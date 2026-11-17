"""用户私有远程 MCP 控制面；连接测试只发现工具，不执行工具。"""

import hashlib
import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import func, select

from app.accounts.access import AccountId
from app.agents.model_config import cipher
from app.audit.service import audit
from app.identity.dependencies import DB, Actor, Writer
from app.providers.client import ProviderFailure
from app.tools.http.contracts import ToolToggle, public_endpoint
from app.tools.mcp.client import MCPClient
from app.tools.mcp.models import MCPCredential, MCPServer, MCPServerVersion
from app.tools.mcp.service import checked_tools, token_value
from app.tools.models import ManagedTool, ToolVersion
from app.tools.router import lock_workspace
from app.tools.service import digest

router = APIRouter(prefix="/api/v1/me/mcp-servers", tags=["MCP"])


class ServerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(max_length=2048)
    transport: Literal["sse", "streamable_http"]
    token: SecretStr | None = None
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def endpoint(cls, value: str) -> str:
        return public_endpoint(value)

    @field_validator("token")
    @classmethod
    def safe_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and (
            len(value.get_secret_value()) > 4096
            or any(not 33 <= ord(c) <= 126 for c in value.get_secret_value())
        ):
            raise ValueError("Token 必须为可见 ASCII 字符，最多 4096 字符")
        return value


def owned(db: DB, user: str, key: str) -> MCPServer:
    row = db.scalar(
        select(MCPServer)
        .where(MCPServer.id == key, MCPServer.account_id == user, MCPServer.deleted.is_(False))
        .with_for_update()
    )
    if row is None:
        raise HTTPException(404)
    return row


def current(db: DB, row: MCPServer) -> MCPServerVersion:
    # 配置版本 ID 不以序号排序，由当前 revision 对应的配置内容定位。
    versions = db.scalars(select(MCPServerVersion).where(MCPServerVersion.server_id == row.id))
    for item in versions:
        if item.config["revision"] == row.revision:
            return item
    raise HTTPException(409, detail={"code": "mcp_configuration_invalid"})


def view(db: DB, row: MCPServer) -> dict[str, Any]:
    version = current(db, row)
    tools = []
    for tool in db.scalars(
        select(ManagedTool).where(
            ManagedTool.account_id == row.account_id,
            ManagedTool.deleted.is_(False),
            ManagedTool.builtin.is_(False),
        )
    ):
        from app.tools.service import latest

        config = latest(db, tool).config
        if config.get("server_id") == row.id:
            tools.append(
                {
                    "name": tool.name,
                    "title": config["title"],
                    "description": config["description"],
                    "enabled": tool.enabled,
                }
            )
    return {
        "id": row.id,
        "name": row.name,
        "enabled": row.enabled,
        "url": version.config["url"],
        "transport": version.config["transport"],
        "has_token": db.get(MCPCredential, row.id) is not None,
        "tools": tools,
    }


def save(db: DB, row: MCPServer, data: ServerInput, request: Request) -> None:
    db.add(
        MCPServerVersion(
            server_id=row.id,
            config={
                "url": data.url,
                "transport": data.transport,
                "revision": row.revision,
            },
        )
    )
    row.name, row.enabled = data.name.strip(), data.enabled
    if not row.name:
        raise HTTPException(422, detail={"code": "mcp_name_required"})
    credential = db.get(MCPCredential, row.id)
    if data.token is not None:
        token = data.token.get_secret_value()
        if credential:
            db.delete(credential)
            db.flush()
        if token:
            encrypted = (
                cipher(request.app.state.settings)
                .encrypt(
                    json.dumps(
                        {
                            "server": row.id,
                            "user": row.account_id,
                            "token": token,
                        }
                    ).encode()
                )
                .decode()
            )
            db.add(MCPCredential(server_id=row.id, encrypted=encrypted))
    db.flush()


@router.get("")
def listing(account_id: AccountId, db: DB, actor: Actor) -> dict[str, Any]:
    rows = db.scalars(
        select(MCPServer)
        .where(MCPServer.account_id == str(account_id), MCPServer.deleted.is_(False))
        .order_by(MCPServer.created_at)
    )
    return {"items": [view(db, row) for row in rows], "next_cursor": None}


@router.post("", status_code=201)
def create(
    data: ServerInput, account_id: AccountId, db: DB, actor: Writer, request: Request
) -> dict[str, Any]:
    user = str(account_id)
    lock_workspace(db, user)
    count = (
        db.scalar(
            select(func.count())
            .select_from(MCPServer)
            .where(MCPServer.account_id == user, MCPServer.deleted.is_(False))
        )
        or 0
    )
    if count >= 10:
        raise HTTPException(409, detail={"code": "mcp_server_limit"})
    row = MCPServer(account_id=user, name=data.name, revision=1)
    db.add(row)
    db.flush()
    save(db, row, data, request)
    audit(db, actor.user, "mcp.create", user, row.id)
    db.commit()
    return view(db, row)


@router.put("/{key}")
def update(
    key: str, data: ServerInput, account_id: AccountId, db: DB, actor: Writer, request: Request
) -> dict[str, Any]:
    lock_workspace(db, str(account_id))
    row = owned(db, str(account_id), key)
    old = current(db, row)
    if data.token is None and data.url != old.config["url"] and db.get(MCPCredential, key):
        raise HTTPException(422, detail={"code": "mcp_new_endpoint_requires_token"})
    if row.revision >= 100:
        raise HTTPException(409, detail={"code": "mcp_version_limit"})
    row.revision += 1
    save(db, row, data, request)
    audit(db, actor.user, "mcp.update", str(account_id), key)
    db.commit()
    return view(db, row)


@router.patch("/{key}")
def toggle(
    key: str, data: ToolToggle, account_id: AccountId, db: DB, actor: Writer
) -> dict[str, Any]:
    row = owned(db, str(account_id), key)
    row.enabled = data.enabled
    audit(db, actor.user, "mcp.toggle", str(account_id), key)
    db.commit()
    return view(db, row)


@router.delete("/{key}", status_code=204)
def remove(key: str, account_id: AccountId, db: DB, actor: Writer) -> Response:
    lock_workspace(db, str(account_id))
    row = owned(db, str(account_id), key)
    row.deleted, row.enabled = True, False
    credential = db.get(MCPCredential, key)
    if credential:
        db.delete(credential)
    from app.tools.service import latest

    for tool in db.scalars(
        select(ManagedTool).where(
            ManagedTool.account_id == row.account_id,
            ManagedTool.builtin.is_(False),
            ManagedTool.deleted.is_(False),
        )
    ):
        if latest(db, tool).config.get("server_id") == key:
            tool.deleted, tool.enabled = True, False
    audit(db, actor.user, "mcp.delete", str(account_id), key)
    db.commit()
    return Response(status_code=204)


@router.post("/{key}/discover")
def discover(
    key: str, account_id: AccountId, db: DB, actor: Writer, request: Request
) -> dict[str, Any]:
    user = str(account_id)
    row = owned(db, user, key)
    if not row.enabled:
        raise HTTPException(409, detail={"code": "mcp_disabled"})
    version = current(db, row)
    revision, version_id, config = row.revision, version.id, dict(version.config)
    token = token_value(db, row, request.app.state.settings)
    db.commit()  # 网络期间释放用户和服务器锁。
    try:
        with MCPClient(config, token) as client:
            tools = checked_tools(client.tools())
    except ProviderFailure as error:
        raise HTTPException(422, detail={"code": error.code}) from error
    except TimeoutError as error:
        raise HTTPException(422, detail={"code": "mcp_timeout"}) from error
    except OSError as error:
        raise HTTPException(422, detail={"code": "mcp_network_failed"}) from error
    except (ValueError, SchemaError) as error:
        raise HTTPException(422, detail={"code": "mcp_tools_invalid"}) from error
    lock_workspace(db, user)
    row = owned(db, user, key)
    if row.revision != revision or not row.enabled:
        raise HTTPException(409, detail={"code": "mcp_configuration_changed"})
    count = (
        db.scalar(
            select(func.count())
            .select_from(ManagedTool)
            .where(ManagedTool.account_id == user, ManagedTool.deleted.is_(False))
        )
        or 0
    )
    from app.tools.service import find_tool, latest

    discovered = []
    for tool in tools:
        name = (
            "mcp_"
            + key.replace("-", "")
            + "_"
            + hashlib.sha256(tool["name"].encode()).hexdigest()[:12]
        )
        existing = find_tool(db, user, name)
        tool_config = {
            "kind": "mcp",
            "server_id": key,
            "server_revision": revision,
            "server_version_id": version_id,
            "remote_name": tool["name"],
            "title": tool["title"],
            "description": tool["description"],
            "inputSchema": tool["inputSchema"],
            "required_roles": ["owner"],
        }
        if existing and latest(db, existing).config == tool_config:
            discovered.append(name)
            continue
        if not existing:
            count += 1
            if count > 100:
                raise HTTPException(409, detail={"code": "tool_catalog_limit"})
            existing = ManagedTool(account_id=user, owner_id=user, name=name, builtin=False)
            db.add(existing)
            db.flush()
            number = 1
        else:
            number = latest(db, existing).number + 1
        if number > 100:
            raise HTTPException(409, detail={"code": "tool_version_limit"})
        existing.enabled = False
        db.add(
            ToolVersion(
                tool_id=existing.id,
                number=number,
                config=tool_config,
                content_hash=digest(tool_config),
            )
        )
        db.flush()
        discovered.append(name)
    for item in db.scalars(
        select(ManagedTool).where(
            ManagedTool.account_id == user,
            ManagedTool.deleted.is_(False),
            ManagedTool.builtin.is_(False),
        )
    ):
        if latest(db, item).config.get("server_id") == key and item.name not in discovered:
            item.enabled = False
    audit(db, actor.user, "mcp.discover", user, key)
    db.commit()
    return view(db, row)
